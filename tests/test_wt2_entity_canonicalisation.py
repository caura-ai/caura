"""WT-2 — one real-world subject must resolve to ONE entity, linked ONCE.

Wet-test evidence (live stack, real LLM):

- memory 1 ``"We decided to use PostgreSQL 16 instead of MySQL for the new
  analytics service."`` → links: ``mysql``, ``new analytics service``,
  ``postgresql 16``
- memory 3 ``"We migrated the analytics service from PostgreSQL 16 to MySQL
  after all."`` → links: ``analytics service``, ``analytics service``
  (DUPLICATE), ``mysql``, ``postgresql 16``

Two defects, two fixes, both covered here:

- **Fix A (link dedup)**: the same ``(memory, entity)`` pair must never be
  written twice from one extraction batch. Deduped at the write site in
  ``entity_extraction_worker`` (the DB composite PK from migration 001 is
  the backstop — no new migration).
- **Fix B (conservative canonicalisation)**: before creating a new entity,
  ``bulk_resolve_entities`` Phase 1.5 compares the incoming name against the
  tenant's existing rows under ``common.entity_naming.canonical_match_key``
  (lowercase / collapse whitespace / strip a small fixed set of leading
  qualifiers, guarded so ``new york`` never merges with ``york``).

Three families:

1. Worker-level (mocked storage) — batch dedupe and link-write dedupe.
2. Storage-level (real Postgres via the in-process ASGI bridge) — Phase 1.5
   normalised matching and its non-merge guards.
3. End-to-end wet-test replay — two extraction runs against real storage,
   asserting one entity row, both memories linked, no duplicate links.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from core_api.constants import VECTOR_DIM
from core_api.services.entity_extraction_worker import process_entity_extraction

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Shared builders (same shapes as tests/test_p1_entity_extraction_bulk.py)
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


def _entity(name: str, entity_type: str = "technology", role: str = "subject") -> MagicMock:
    e = MagicMock()
    e.canonical_name = name
    e.entity_type = entity_type
    e.role = role
    return e


def _graph(entities: list[MagicMock], relations: list | None = None) -> MagicMock:
    g = MagicMock()
    g.entities = entities
    g.relations = relations or []
    return g


def _build_sc_mock(
    *,
    resolve_returns: list[dict | None],
    upsert_returns: list[dict],
) -> MagicMock:
    sc = MagicMock()
    sc.bulk_resolve_entities = AsyncMock(return_value=resolve_returns)
    sc.bulk_upsert_entities = AsyncMock(return_value=upsert_returns)
    sc.bulk_upsert_entity_links = AsyncMock(
        return_value=[{"input_idx": i, "created": True} for i in range(len(upsert_returns))]
    )
    return sc


_WORKER = "core_api.services.entity_extraction_worker"


# ---------------------------------------------------------------------------
# 1) Worker-level — mocked storage
# ---------------------------------------------------------------------------


@pytest.mark.unit
@patch(f"{_WORKER}.log_action", new_callable=AsyncMock)
@patch(f"{_WORKER}.upsert_relation", new_callable=AsyncMock)
@patch(f"{_WORKER}.get_embedding", new_callable=AsyncMock)
@patch(f"{_WORKER}.get_storage_client")
@patch(f"{_WORKER}.extract_entities_from_content", new_callable=AsyncMock)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_extractor_repeating_one_entity_writes_one_link(
    mock_cfg, mock_extract, mock_sc_factory, mock_embed, _rel, _log
):
    """The wet-test memory-3 shape: the extractor returns the SAME subject
    twice (verbatim + a case variant). Exactly ONE entity is resolved /
    upserted and exactly ONE link row is written."""
    mock_cfg.return_value = _config()
    mock_extract.return_value = _graph(
        [
            _entity("analytics service", role="subject"),
            _entity("Analytics Service", role="mentioned"),
        ]
    )
    mock_embed.return_value = None  # no similarity phase in this test

    eid = str(uuid4())
    sc = _build_sc_mock(
        resolve_returns=[None],
        upsert_returns=[{"input_idx": 0, "entity_id": eid, "action": "created"}],
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=lambda coro: coro.close()):
        await process_entity_extraction(
            uuid4(), "t-wt2", None, "agent-1", "content", "decision"
        )

    # One resolve item, one upsert item, ONE link item.
    resolve_items = sc.bulk_resolve_entities.call_args.kwargs["items"]
    assert len(resolve_items) == 1
    link_items = sc.bulk_upsert_entity_links.call_args.kwargs["items"]
    assert len(link_items) == 1
    assert link_items[0]["entity_id"] == eid
    assert link_items[0]["role"] == "subject"  # first occurrence wins


@pytest.mark.unit
@patch(f"{_WORKER}.log_action", new_callable=AsyncMock)
@patch(f"{_WORKER}.upsert_relation", new_callable=AsyncMock)
@patch(f"{_WORKER}.get_embedding", new_callable=AsyncMock)
@patch(f"{_WORKER}.get_storage_client")
@patch(f"{_WORKER}.extract_entities_from_content", new_callable=AsyncMock)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_qualifier_variants_in_one_extraction_collapse_to_one_entity(
    mock_cfg, mock_extract, mock_sc_factory, mock_embed, _rel, _log
):
    """``new analytics service`` and ``analytics service`` in ONE extraction
    are the same subject — one entity minted, one link written."""
    mock_cfg.return_value = _config()
    mock_extract.return_value = _graph(
        [
            _entity("new analytics service"),
            _entity("analytics service"),
            _entity("postgresql 16"),
        ]
    )
    mock_embed.return_value = None

    svc_id, pg_id = str(uuid4()), str(uuid4())
    sc = _build_sc_mock(
        resolve_returns=[None, None],
        upsert_returns=[
            {"input_idx": 0, "entity_id": svc_id, "action": "created"},
            {"input_idx": 1, "entity_id": pg_id, "action": "created"},
        ],
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=lambda coro: coro.close()):
        await process_entity_extraction(
            uuid4(), "t-wt2", None, "agent-1", "content", "decision"
        )

    resolve_items = sc.bulk_resolve_entities.call_args.kwargs["items"]
    assert [it["canonical_name"] for it in resolve_items] == [
        "new analytics service",  # first surface form wins
        "postgresql 16",
    ]
    link_items = sc.bulk_upsert_entity_links.call_args.kwargs["items"]
    assert len(link_items) == 2
    assert {it["entity_id"] for it in link_items} == {svc_id, pg_id}


@pytest.mark.unit
@patch(f"{_WORKER}.log_action", new_callable=AsyncMock)
@patch(f"{_WORKER}.upsert_relation", new_callable=AsyncMock)
@patch(f"{_WORKER}.get_embedding", new_callable=AsyncMock)
@patch(f"{_WORKER}.get_storage_client")
@patch(f"{_WORKER}.extract_entities_from_content", new_callable=AsyncMock)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_two_names_resolving_to_same_entity_write_one_link(
    mock_cfg, mock_extract, mock_sc_factory, mock_embed, _rel, _log
):
    """Write-site dedup proper (Fix A): two names with DIFFERENT match keys
    that storage resolution nevertheless maps to the same entity row (e.g.
    embedding similarity) must produce ONE link item, not two."""
    mock_cfg.return_value = _config()
    mock_extract.return_value = _graph(
        [
            _entity("postgres"),
            _entity("postgresql"),
        ]
    )
    mock_embed.return_value = [0.1] * 8

    shared = str(uuid4())
    sc = _build_sc_mock(
        resolve_returns=[
            {"entity_id": shared, "canonical_name": "postgres", "attributes": {}, "matched_by": "similarity", "similarity": 0.97},
            {"entity_id": shared, "canonical_name": "postgres", "attributes": {}, "matched_by": "similarity", "similarity": 0.95},
        ],
        upsert_returns=[
            {"input_idx": 0, "entity_id": shared, "action": "updated"},
            {"input_idx": 1, "entity_id": shared, "action": "updated"},
        ],
    )
    mock_sc_factory.return_value = sc

    memory_id = uuid4()
    with patch("core_api.tasks.track_task", side_effect=lambda coro: coro.close()):
        await process_entity_extraction(
            memory_id, "t-wt2", None, "agent-1", "content", "decision"
        )

    link_items = sc.bulk_upsert_entity_links.call_args.kwargs["items"]
    assert len(link_items) == 1, (
        f"duplicate (memory, entity) link items written: {link_items}"
    )
    assert link_items[0]["memory_id"] == str(memory_id)
    assert link_items[0]["entity_id"] == shared
    # input_idx must still tile [0, len) for the storage-side contiguity check.
    assert [it["input_idx"] for it in link_items] == [0]


# ---------------------------------------------------------------------------
# 2) Storage-level — Phase 1.5 normalised match (real Postgres)
# ---------------------------------------------------------------------------


def _t() -> str:
    return f"test-tenant-wt2-{uuid4().hex[:8]}"


async def _create_entity(sc, tenant_id: str, name: str, *, entity_type: str = "technology") -> dict:
    return await sc.create_entity(
        {
            "tenant_id": tenant_id,
            "fleet_id": None,
            "entity_type": entity_type,
            "canonical_name": name,
            "attributes": {},
            "name_embedding": None,
        }
    )


def _resolve_item(name: str, *, entity_type: str = "technology") -> dict:
    return {
        "input_idx": 0,
        "fleet_id": None,
        "canonical_name": name,
        "entity_type": entity_type,
        "name_embedding": None,
    }


@pytest.mark.integration
async def test_bulk_resolve_normalized_match_strips_leading_qualifier(sc):
    """Incoming ``analytics service`` resolves to the existing
    ``new analytics service`` row instead of minting a second entity."""
    tid = _t()
    existing = await _create_entity(sc, tid, "new analytics service")

    results = await sc.bulk_resolve_entities(
        tenant_id=tid, items=[_resolve_item("analytics service")], threshold=0.85
    )
    assert results[0] is not None
    assert results[0]["entity_id"] == existing["id"]
    assert results[0]["matched_by"] == "normalized"


@pytest.mark.integration
async def test_bulk_resolve_normalized_match_is_symmetric(sc):
    """Reverse direction: incoming ``new analytics service`` resolves to an
    existing ``analytics service`` row."""
    tid = _t()
    existing = await _create_entity(sc, tid, "analytics service")

    results = await sc.bulk_resolve_entities(
        tenant_id=tid, items=[_resolve_item("new analytics service")], threshold=0.85
    )
    assert results[0] is not None
    assert results[0]["entity_id"] == existing["id"]
    assert results[0]["matched_by"] == "normalized"


@pytest.mark.integration
async def test_bulk_resolve_normalized_match_case_and_whitespace(sc):
    tid = _t()
    existing = await _create_entity(sc, tid, "analytics service")

    results = await sc.bulk_resolve_entities(
        tenant_id=tid, items=[_resolve_item("Analytics  Service")], threshold=0.85
    )
    assert results[0] is not None
    assert results[0]["entity_id"] == existing["id"]


@pytest.mark.integration
async def test_bulk_resolve_new_york_never_merges_with_york(sc):
    """The non-merge guard, both directions. ``new york`` stripped would be
    the 1-token ``york``, so the qualifier is treated as part of the name."""
    tid = _t()
    await _create_entity(sc, tid, "new york", entity_type="location")

    results = await sc.bulk_resolve_entities(
        tenant_id=tid,
        items=[_resolve_item("york", entity_type="location")],
        threshold=0.85,
    )
    assert results[0] is None, "incoming 'york' must NOT merge into 'new york'"

    tid2 = _t()
    await _create_entity(sc, tid2, "york", entity_type="location")
    results = await sc.bulk_resolve_entities(
        tenant_id=tid2,
        items=[_resolve_item("new york", entity_type="location")],
        threshold=0.85,
    )
    assert results[0] is None, "incoming 'new york' must NOT merge into 'york'"


@pytest.mark.integration
async def test_bulk_resolve_normalized_match_respects_entity_type(sc):
    """Same scoping as Phase 1 exact: a name match across DIFFERENT
    entity_types is not a merge."""
    tid = _t()
    await _create_entity(sc, tid, "new analytics service", entity_type="technology")

    results = await sc.bulk_resolve_entities(
        tenant_id=tid,
        items=[_resolve_item("analytics service", entity_type="project")],
        threshold=0.85,
    )
    assert results[0] is None


@pytest.mark.integration
async def test_bulk_resolve_no_substring_or_extra_word_merge(sc):
    """Non-qualifier leading words never strip; substrings never match."""
    tid = _t()
    await _create_entity(sc, tid, "data analytics service")

    for incoming in ("analytics service", "service", "data analytics"):
        results = await sc.bulk_resolve_entities(
            tenant_id=tid, items=[_resolve_item(incoming)], threshold=0.85
        )
        assert results[0] is None, f"'{incoming}' must not merge into 'data analytics service'"


@pytest.mark.integration
async def test_bulk_resolve_exact_match_still_wins_over_normalized(sc):
    """With both ``new york`` and ``york`` present, each incoming form
    resolves exactly to its own row."""
    tid = _t()
    ny = await _create_entity(sc, tid, "new york", entity_type="location")
    y = await _create_entity(sc, tid, "york", entity_type="location")

    results = await sc.bulk_resolve_entities(
        tenant_id=tid,
        items=[
            {**_resolve_item("new york", entity_type="location"), "input_idx": 0},
            {**_resolve_item("york", entity_type="location"), "input_idx": 1},
        ],
        threshold=0.85,
    )
    assert results[0]["entity_id"] == ny["id"]
    assert results[0]["matched_by"] == "exact"
    assert results[1]["entity_id"] == y["id"]
    assert results[1]["matched_by"] == "exact"


# ---------------------------------------------------------------------------
# 3) End-to-end wet-test replay — real storage, mocked extractor output
# ---------------------------------------------------------------------------


async def _write_memory(sc, tenant_id: str, content: str) -> dict:
    return await sc.create_memory(
        {
            "tenant_id": tenant_id,
            "agent_id": "wt2-agent",
            "content": content,
            "memory_type": "decision",
            "embedding": [0.1] * VECTOR_DIM,
            "weight": 0.5,
        }
    )


@pytest.mark.integration
@patch(f"{_WORKER}.log_action", new_callable=AsyncMock)
@patch(f"{_WORKER}.get_embedding", new_callable=AsyncMock)
@patch(f"{_WORKER}.extract_entities_from_content", new_callable=AsyncMock)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_wet_test_replay_one_subject_one_entity_no_duplicate_links(
    mock_cfg, mock_extract, mock_embed, _log, sc
):
    """Replays WT-2 against real storage: memory 1 extracts ``new analytics
    service``; memory 3 extracts ``analytics service`` TWICE. Expected end
    state: ONE entity row for the subject, both memories linked to it, one
    link row each, and the second surface form recorded as an alias."""
    tid = _t()
    mock_cfg.return_value = _config()
    mock_embed.return_value = None  # deterministic phases only (no similarity)

    mem1 = await _write_memory(
        sc, tid, "We decided to use PostgreSQL 16 instead of MySQL for the new analytics service."
    )
    mem3 = await _write_memory(
        sc, tid, "We migrated the analytics service from PostgreSQL 16 to MySQL after all."
    )

    # Memory 1 extraction.
    mock_extract.return_value = _graph(
        [
            _entity("postgresql 16"),
            _entity("mysql"),
            _entity("new analytics service"),
        ]
    )
    with patch("core_api.tasks.track_task", side_effect=lambda coro: coro.close()):
        await process_entity_extraction(
            UUID(mem1["id"]), tid, None, "wt2-agent", mem1["content"], "decision"
        )

    # Memory 3 extraction — the same subject twice, without the qualifier.
    mock_extract.return_value = _graph(
        [
            _entity("analytics service", role="subject"),
            _entity("analytics service", role="mentioned"),
            _entity("mysql"),
            _entity("postgresql 16"),
        ]
    )
    with patch("core_api.tasks.track_task", side_effect=lambda coro: coro.close()):
        await process_entity_extraction(
            UUID(mem3["id"]), tid, None, "wt2-agent", mem3["content"], "decision"
        )

    # ONE entity row for the subject: the first-seen canonical name stands,
    # and no second row was minted for the stripped form.
    subject = await sc.find_exact_entity(
        tid, "new analytics service", None, entity_type="technology"
    )
    assert subject is not None
    split_row = await sc.find_exact_entity(
        tid, "analytics service", None, entity_type="technology"
    )
    assert split_row is None, "WT-2 regression: subject split into a second entity row"

    # The memory-3 surface form is preserved as an alias.
    assert "analytics service" in (subject.get("attributes") or {}).get("_aliases", [])

    # Both memories linked to the ONE subject entity — once each.
    result = await sc.get_entity_with_linked_memories(subject["id"])
    linked_ids = [entry.get("memory", entry)["id"] for entry in result["linked_memories"]]
    assert sorted(linked_ids) == sorted([mem1["id"], mem3["id"]])
    assert len(linked_ids) == len(set(linked_ids)), "duplicate link rows"
