"""Audit P1 — collapse N+1 storage HTTPs in the entity-extraction worker.

The pre-fix path issued up to ~6 HTTPs per entity (1 embed + up to 2 in
``upsert_entity`` find/create + up to 2 in find/create-link).

After P1 and L-181:
  - one ``sc.bulk_resolve_entities`` HTTP that resolves every name by name
  - one ``get_embeddings_batch`` call for the names that left unmatched
  - one ``sc.bulk_resolve_entities`` HTTP that resolves those by similarity
  - one ``sc.bulk_upsert_entities`` HTTP
  - one ``sc.bulk_upsert_entity_links`` HTTP

Two test families:

1. **Shape**: assert the wire effect directly — exactly one of each
   bulk call per memory, zero per-row HTTPs.

2. **State-equivalence**: replicate the ``entity_service.upsert_entity``
   merge contract end-to-end:
     - first-seen-wins canonical_name (the audit's correctness gate,
       see entity_service.py:91-100 for the prior longest-wins
       regression that this rule guards against)
     - alias accumulation in ``attributes["_aliases"]``
     - exact match overrides similarity match
     - Phase 2 skipped when no embedding
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core_api.services.entity_extraction_worker import process_entity_extraction
from tests.conftest import close_scheduled_coro

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------


def _config(**overrides):
    cfg = MagicMock()
    cfg.auto_entity_linking_enabled = False
    cfg.entity_blocklist = frozenset()
    cfg.entity_extraction_provider = "openai"
    cfg.entity_extraction_model = "gpt-4o-mini"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _entity(name: str, entity_type: str = "person", role: str = "subject") -> MagicMock:
    e = MagicMock()
    e.canonical_name = name
    e.entity_type = entity_type
    e.role = role
    return e


def _graph(entities: list[MagicMock], relations: list = None) -> MagicMock:
    g = MagicMock()
    g.entities = entities
    g.relations = relations or []
    return g


def _build_sc_mock(
    *,
    resolve_returns: list[dict | None],
    upsert_returns: list[dict],
    links_returns: list[dict] | None = None,
) -> MagicMock:
    sc = MagicMock()
    # H-02: the worker re-reads the memory before persisting, so nothing is
    # written to the graph of a row governance dropped mid-extraction. These
    # tests exercise a live row.
    sc.get_memory = AsyncMock(return_value={"id": "m", "deleted_at": None})
    # ``resolve_returns`` is aligned to the names the first call sends. Later
    # calls are answered by name, and a similarity match only to an item that
    # carries a vector, as storage's Phase 2 does (L-181).
    by_name: dict[str, dict | None] = {}

    async def resolve(*, tenant_id, items, threshold):
        if not by_name:
            names = [item["canonical_name"] for item in items]
            by_name.update(zip(names, resolve_returns, strict=True))
        answers = []
        for item in items:
            match = by_name.get(item["canonical_name"])
            if match and match["matched_by"] == "similarity":
                match = match if item["name_embedding"] is not None else None
            answers.append(match)
        return answers

    sc.bulk_resolve_entities = AsyncMock(side_effect=resolve)
    sc.bulk_upsert_entities = AsyncMock(return_value=upsert_returns)
    sc.bulk_upsert_entity_links = AsyncMock(
        return_value=links_returns
        or [{"input_idx": i, "created": True} for i in range(len(upsert_returns))]
    )
    # Spies for the legacy per-row paths — must stay at zero call_count.
    sc.find_exact_entity = AsyncMock()
    sc.find_by_embedding_similarity = AsyncMock()
    sc.update_entity = AsyncMock()
    sc.create_entity = AsyncMock()
    return sc


def _vectors(names, *_args, **_kwargs):
    """``get_embeddings_batch``: one vector per name."""
    return [[0.1] * 10 for _ in names]


# ---------------------------------------------------------------------------
# Shape tests — one HTTP per stage
# ---------------------------------------------------------------------------


@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.get_embeddings_batch",
    new_callable=AsyncMock,
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_bulk_path_collapses_to_3_storage_https_for_n_entities(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, _rel, _log
):
    """Three new entities → one bulk_resolve by name, one by similarity for
    the three it left, one bulk_upsert and one bulk_upsert_entity_links. Zero
    per-row find/create calls."""
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph(
        [_entity("alice"), _entity("bob"), _entity("carol")]
    )
    mock_embed.side_effect = _vectors

    sc = _build_sc_mock(
        resolve_returns=[None, None, None],  # all new (no match)
        upsert_returns=[
            {"input_idx": 0, "entity_id": str(uuid4()), "action": "created"},
            {"input_idx": 1, "entity_id": str(uuid4()), "action": "created"},
            {"input_idx": 2, "entity_id": str(uuid4()), "action": "created"},
        ],
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content="alice met bob and carol",
            memory_type="episodic",
        )

    assert sc.bulk_resolve_entities.call_count == 2
    assert sc.bulk_upsert_entities.call_count == 1
    assert sc.bulk_upsert_entity_links.call_count == 1
    # Legacy per-row paths must be untouched.
    assert sc.find_exact_entity.call_count == 0
    assert sc.create_entity.call_count == 0


@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.get_embeddings_batch",
    new_callable=AsyncMock,
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_unmatched_names_are_embedded_in_one_batch(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, _rel, _log
):
    """The names the by-name pass left go to the provider in one
    ``get_embeddings_batch`` call, not one call each (L-181)."""
    mock_resolve.return_value = _config()
    names = ["alice", "bob", "carol", "dave"]
    mock_extract.return_value = _graph([_entity(n) for n in names])
    mock_embed.side_effect = _vectors
    sc = _build_sc_mock(
        resolve_returns=[None] * 4,
        upsert_returns=[
            {"input_idx": i, "entity_id": str(uuid4()), "action": "created"}
            for i in range(4)
        ],
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content=" ".join(names),
            memory_type="episodic",
        )

    mock_embed.assert_awaited_once()
    assert mock_embed.await_args.args[0] == names


@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.get_embeddings_batch",
    new_callable=AsyncMock,
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_zero_entities_skips_bulk_https_entirely(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, _rel, _log
):
    """Empty extracted graph → no bulk HTTPs (early return)."""
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph([])  # no entities

    sc = _build_sc_mock(resolve_returns=[], upsert_returns=[])
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content="empty content",
            memory_type="episodic",
        )

    assert sc.bulk_resolve_entities.call_count == 0
    assert sc.bulk_upsert_entities.call_count == 0
    assert sc.bulk_upsert_entity_links.call_count == 0
    assert mock_embed.call_count == 0  # nothing embedded either


@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.get_embeddings_batch",
    new_callable=AsyncMock,
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_blocklisted_entities_filtered_before_bulk_path(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, _rel, _log
):
    """Blocklisted names dropped from the resolve / upsert / link
    batches — payloads carry only the survivors."""
    mock_resolve.return_value = _config(entity_blocklist=frozenset({"team", "system"}))
    mock_extract.return_value = _graph(
        [_entity("alice"), _entity("team"), _entity("bob"), _entity("system")]
    )
    mock_embed.side_effect = _vectors

    sc = _build_sc_mock(
        resolve_returns=[None, None],
        upsert_returns=[
            {"input_idx": 0, "entity_id": str(uuid4()), "action": "created"},
            {"input_idx": 1, "entity_id": str(uuid4()), "action": "created"},
        ],
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content="alice and bob from system team",
            memory_type="episodic",
        )

    payload = sc.bulk_resolve_entities.await_args_list[0].kwargs
    names = [it["canonical_name"] for it in payload["items"]]
    assert names == ["alice", "bob"]  # blocklisted survivors removed, order preserved


@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.get_embeddings_batch",
    new_callable=AsyncMock,
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_duplicate_canonical_names_collapse_to_first(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, _rel, _log
):
    """LLM occasionally returns the same canonical_name twice (across
    mentions). Old serial path collapsed via the ``name_to_id`` dict;
    bulk path collapses up-front in the filter step so the resolve /
    upsert / link batches don't carry duplicates.

    First-occurrence role wins (matches the old code's
    ``entity_roles[ent.canonical_name] = ent.role`` which was
    first-write-wins via dict semantics)."""
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph(
        [
            _entity("alice", role="subject"),
            _entity("alice", role="object"),  # duplicate
            _entity("bob"),
        ]
    )
    mock_embed.side_effect = _vectors

    sc = _build_sc_mock(
        resolve_returns=[None, None],
        upsert_returns=[
            {"input_idx": 0, "entity_id": str(uuid4()), "action": "created"},
            {"input_idx": 1, "entity_id": str(uuid4()), "action": "created"},
        ],
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content="alice met bob",
            memory_type="episodic",
        )

    resolve_payload = sc.bulk_resolve_entities.await_args_list[0].kwargs
    names = [it["canonical_name"] for it in resolve_payload["items"]]
    assert names == ["alice", "bob"]

    link_payload = sc.bulk_upsert_entity_links.call_args.kwargs["items"]
    alice_links = [li for li in link_payload if li["input_idx"] == 0]
    assert len(alice_links) == 1
    assert alice_links[0]["role"] == "subject"  # first-occurrence role


# ---------------------------------------------------------------------------
# State-equivalence tests — replicate entity_service.upsert_entity contract
# ---------------------------------------------------------------------------


@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.get_embeddings_batch",
    new_callable=AsyncMock,
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_first_seen_wins_canonical_when_longer_name_arrives(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, _rel, _log
):
    """Regression guard for the prior longest-wins bug (see
    ``entity_service.py:91-100``). Existing row has ``canonical_name=
    "globex"``; the new memory mentions ``"globex industries"``;
    similarity merges the two. The upsert payload MUST set
    ``canonical_name="globex"`` (first-seen wins), with
    ``"globex industries"`` accumulated in ``_aliases``."""
    existing_entity_id = str(uuid4())
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph([_entity("globex industries", "organization")])
    mock_embed.side_effect = _vectors

    sc = _build_sc_mock(
        # bulk_resolve returns the existing ``globex`` row matched by similarity.
        resolve_returns=[
            {
                "entity_id": existing_entity_id,
                "canonical_name": "globex",
                "attributes": {},
                "matched_by": "similarity",
                "similarity": 0.92,
            }
        ],
        upsert_returns=[
            {"input_idx": 0, "entity_id": existing_entity_id, "action": "updated"}
        ],
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content="globex industries shipped a new product",
            memory_type="episodic",
        )

    upsert_payload = sc.bulk_upsert_entities.call_args.kwargs["items"][0]
    assert upsert_payload["action"] == "update"
    assert upsert_payload["entity_id"] == existing_entity_id
    # First-seen-wins — existing ``globex`` stays canonical.
    assert upsert_payload["canonical_name"] == "globex"
    # The longer surface form lands in ``_aliases``, not as canonical.
    aliases = upsert_payload["attributes"]["_aliases"]
    assert "globex" in aliases
    assert "globex industries" in aliases


@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.get_embeddings_batch",
    new_callable=AsyncMock,
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_existing_aliases_preserved_and_extended(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, _rel, _log
):
    """The update item names the existing ``canonical_name`` and the new
    surface form as aliases. Storage unions them into the stored list, which is
    preserved verbatim (L-46), so the payload no longer copies it."""
    existing_entity_id = str(uuid4())
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph([_entity("acme co", "organization")])
    mock_embed.side_effect = _vectors

    sc = _build_sc_mock(
        resolve_returns=[
            {
                "entity_id": existing_entity_id,
                "canonical_name": "acme",
                "attributes": {"_aliases": ["acme", "acme corp"]},
                "matched_by": "similarity",
                "similarity": 0.91,
            }
        ],
        upsert_returns=[
            {"input_idx": 0, "entity_id": existing_entity_id, "action": "updated"}
        ],
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content="acme co",
            memory_type="episodic",
        )

    aliases = sc.bulk_upsert_entities.call_args.kwargs["items"][0]["attributes"][
        "_aliases"
    ]
    # The item carries the stored canonical name and the new form; storage
    # unions them into the row's aliases, so "acme corp" stays without being
    # sent (L-46, tests/test_entity_upsert_merges.py).
    assert aliases == ["acme", "acme co"]


@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.get_embeddings_batch",
    new_callable=AsyncMock,
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_no_match_takes_create_path(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, _rel, _log
):
    """``bulk_resolve_entities`` returns ``None`` for a name with no
    existing match — the upsert payload uses ``action="create"`` with
    the new canonical_name and an empty attributes dict (consistent
    with ``entity_service.create_data["attributes"] = data.attributes
    or {}``)."""
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph([_entity("brand-new-entity", "person")])
    mock_embed.side_effect = _vectors

    sc = _build_sc_mock(
        resolve_returns=[None],
        upsert_returns=[
            {"input_idx": 0, "entity_id": str(uuid4()), "action": "created"}
        ],
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content="brand-new-entity appeared",
            memory_type="episodic",
        )

    item = sc.bulk_upsert_entities.call_args.kwargs["items"][0]
    assert item["action"] == "create"
    assert item["canonical_name"] == "brand-new-entity"
    assert item["attributes"] == {}
    assert "entity_id" not in item


@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.get_embeddings_batch",
    new_callable=AsyncMock,
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_a_name_that_fails_to_embed_is_created_without_a_vector(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, _rel, _log
):
    """A ``None`` slot from the embedder means no vector for that name. It is
    left out of the similarity pass, since storage's Phase 2 needs a vector,
    and takes the create path without one; the other name is unaffected. Same
    skip semantic as the old per-entity try/except."""
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph([_entity("alice"), _entity("bob")])

    def alice_only(names, *_args, **_kwargs):
        return [[0.1] * 10 if name == "alice" else None for name in names]

    mock_embed.side_effect = alice_only

    sc = _build_sc_mock(
        resolve_returns=[None, None],
        upsert_returns=[
            {"input_idx": 0, "entity_id": str(uuid4()), "action": "created"},
            {"input_idx": 1, "entity_id": str(uuid4()), "action": "created"},
        ],
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content="alice met bob",
            memory_type="episodic",
        )

    sent = [
        [
            (it["canonical_name"], it["name_embedding"] is not None)
            for it in c.kwargs["items"]
        ]
        for c in sc.bulk_resolve_entities.await_args_list
    ]
    assert sent == [[("alice", False), ("bob", False)], [("alice", True)]]
    items = sc.bulk_upsert_entities.call_args.kwargs["items"]
    by_name = {it["canonical_name"]: it for it in items}
    assert by_name["alice"]["name_embedding"] is not None
    assert "name_embedding" not in by_name["bob"]  # embed failed → no vector


@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.get_embeddings_batch",
    new_callable=AsyncMock,
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_link_role_binding_preserved_per_entity(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, _rel, _log
):
    """The role attached to each entity (subject/object/mentioned)
    must follow that entity into the link upsert — the prior bug
    surface was the old ``name_to_id.items()`` loop which lost the
    explicit role binding by dict iteration order."""
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph(
        [
            _entity("alice", role="subject"),
            _entity("bob", role="object"),
            _entity("carol", role="mentioned"),
        ]
    )
    mock_embed.side_effect = _vectors

    eids = [str(uuid4()) for _ in range(3)]
    sc = _build_sc_mock(
        resolve_returns=[None, None, None],
        upsert_returns=[
            {"input_idx": 0, "entity_id": eids[0], "action": "created"},
            {"input_idx": 1, "entity_id": eids[1], "action": "created"},
            {"input_idx": 2, "entity_id": eids[2], "action": "created"},
        ],
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content="alice met bob, mentioning carol",
            memory_type="episodic",
        )

    link_items = sc.bulk_upsert_entity_links.call_args.kwargs["items"]
    by_eid = {li["entity_id"]: li for li in link_items}
    assert by_eid[eids[0]]["role"] == "subject"
    assert by_eid[eids[1]]["role"] == "object"
    assert by_eid[eids[2]]["role"] == "mentioned"


@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.get_embeddings_batch",
    new_callable=AsyncMock,
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_threshold_constant_passed_to_resolve(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, _rel, _log
):
    """The worker hands ``ENTITY_RESOLUTION_THRESHOLD`` to the storage
    endpoint — the resolution rule lives in the core-api layer, not
    the storage executor (see ``/entities/bulk-resolve`` docstring)."""
    from core_api.constants import ENTITY_RESOLUTION_THRESHOLD

    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph([_entity("alice")])
    mock_embed.side_effect = _vectors

    sc = _build_sc_mock(
        resolve_returns=[None],
        upsert_returns=[
            {"input_idx": 0, "entity_id": str(uuid4()), "action": "created"}
        ],
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content="alice",
            memory_type="episodic",
        )

    kwargs = sc.bulk_resolve_entities.call_args.kwargs
    assert kwargs["threshold"] == ENTITY_RESOLUTION_THRESHOLD
