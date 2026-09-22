"""L-32 — two surface forms, one entity row, one alias silently lost.

One extraction can name the same real-world entity twice in different words —
"IBM" and "I.B.M.", "Acme" and "Acme Corp". The WT-2 dedupe in
``process_entity_extraction`` does not collapse them, and is right not to: it
compares ``canonical_match_key``s, which for those pairs genuinely differ. They
survive as two entries in ``filtered``.

``/entities/bulk-resolve`` then answers each one independently, and both can
land on the SAME existing row — one by exact match, the other by embedding
similarity. The worker built an upsert item for each, and each computed its
``_aliases`` from the SAME resolve snapshot, so neither payload contained the
other's alias. Storage applies ``attributes`` wholesale per item, in list order
(``entity_bulk_upsert``: "caller pre-computed the merged attributes — server
side does not re-merge"), so the second item overwrote the first's:

    item 0  attributes._aliases = ["IBM Corporation", "IBM"]
    item 1  attributes._aliases = ["IBM Corporation", "I.B.M."]   ← wins

and "IBM" was written and then destroyed inside a single batch. Nothing raced:
the loop is sequential and the storage side applies updates in list order, so
it is LAST-WINS and perfectly reproducible — which is part of why it never
looked like a bug worth chasing.

The fix coalesces the two items into one before the wire, so the row takes one
UPDATE carrying both aliases. Everything else stays first-seen-wins:
``canonical_name``, ``entity_type``, ``name_embedding``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core_api.services.entity_extraction_worker import process_entity_extraction
from tests.conftest import close_scheduled_coro
from tests.test_p1_entity_extraction_bulk import (
    _build_sc_mock,
    _config,
    _entity,
    _graph,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

# The row both surface forms resolve to.
EXISTING_ID = str(uuid4())
EXISTING_NAME = "IBM Corporation"


def _match(
    *, canonical_name: str = EXISTING_NAME, attributes: dict | None = None
) -> dict:
    return {
        "entity_id": EXISTING_ID,
        "canonical_name": canonical_name,
        "attributes": attributes
        if attributes is not None
        else {"_aliases": [EXISTING_NAME]},
        "matched_by": "similarity",
        "similarity": 0.91,
    }


def _relation(frm: str, rel_type: str, to: str) -> MagicMock:
    r = MagicMock()
    r.from_entity = frm
    r.relation_type = rel_type
    r.to_entity = to
    return r


def _worker_patches(fn):
    """Applied in listed order, so ``resolve_config`` is innermost and its mock
    is the first injected argument — the bottom-up order a stacked ``@patch``
    gives. Tests take them as ``(mock_resolve, mock_extract, mock_sc_factory,
    _embed, _log, mock_rel)``."""
    for deco in (
        patch(
            "core_api.services.organization_settings.resolve_config",
            new_callable=AsyncMock,
        ),
        patch(
            "core_api.services.entity_extraction_worker.extract_entities_from_content",
            new_callable=AsyncMock,
        ),
        patch("core_api.services.entity_extraction_worker.get_storage_client"),
        patch(
            "core_api.services.entity_extraction_worker.get_embedding",
            new_callable=AsyncMock,
        ),
        patch(
            "core_api.services.entity_extraction_worker.log_action",
            new_callable=AsyncMock,
        ),
        patch(
            "core_api.services.entity_extraction_worker.bulk_upsert_relations",
            new_callable=AsyncMock,
        ),
    ):
        fn = deco(fn)
    return fn


def _sc(resolve_returns: list[dict | None]) -> MagicMock:
    sc = _build_sc_mock(
        resolve_returns=resolve_returns,
        # One row comes back because one row is what the fixed worker asks for.
        # ``input_idx`` 0 is the coalesced item.
        upsert_returns=[
            {"input_idx": 0, "entity_id": EXISTING_ID, "action": "updated"}
        ],
    )
    sc.set_subject_entity_if_null = AsyncMock(return_value=True)
    sc.set_predicate_if_null = AsyncMock(return_value=True)
    return sc


async def _run() -> None:
    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content="IBM shipped it. I.B.M. announced it the same day.",
            memory_type="episodic",
        )


def _items_for(sc: MagicMock, entity_id: str) -> list[dict]:
    items = sc.bulk_upsert_entities.await_args.kwargs["items"]
    return [i for i in items if i.get("entity_id") == entity_id]


@_worker_patches
async def test_both_surface_forms_survive_the_merge(
    mock_resolve, mock_extract, mock_sc_factory, _embed, _log, _rel
):
    """The defect itself. BOTH aliases must reach storage.

    Pre-fix the batch carried two items for one row and the second's
    ``_aliases`` — which never saw "IBM" — was the one that stuck.
    """
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph(
        [_entity("IBM", "organization"), _entity("I.B.M.", "organization")]
    )
    sc = _sc([_match(), _match()])
    mock_sc_factory.return_value = sc

    await _run()

    items = _items_for(sc, EXISTING_ID)
    assert len(items) == 1, (
        f"one row must take one UPDATE, not {len(items)} — two items for the "
        f"same entity_id is the lost update"
    )
    aliases = items[0]["attributes"]["_aliases"]
    assert "IBM" in aliases, "the first surface form's alias was overwritten"
    assert "I.B.M." in aliases, "the second surface form's alias is missing"
    assert EXISTING_NAME in aliases, "the row's own canonical name must stay an alias"


@_worker_patches
async def test_the_merge_preserves_the_rows_other_attributes(
    mock_resolve, mock_extract, mock_sc_factory, _embed, _log, _rel
):
    """Coalescing must not become a way to drop everything except ``_aliases``.

    A fix that rebuilt the payload from the second form alone would also make
    the alias assertion above pass while wiping the row's real attributes.
    """
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph(
        [_entity("IBM", "organization"), _entity("I.B.M.", "organization")]
    )
    attrs = {"_aliases": [EXISTING_NAME], "industry": "technology", "founded": "1911"}
    sc = _sc([_match(attributes=attrs), _match(attributes=attrs)])
    mock_sc_factory.return_value = sc

    await _run()

    merged = _items_for(sc, EXISTING_ID)[0]["attributes"]
    assert merged["industry"] == "technology"
    assert merged["founded"] == "1911"


@_worker_patches
async def test_first_seen_still_wins_the_canonical_name(
    mock_resolve, mock_extract, mock_sc_factory, _embed, _log, _rel
):
    """The correctness gate the whole merge is built around.

    ``entity_service.upsert_entity`` keeps the EXISTING canonical name when a
    row is found — the rule that stops an LLM's surface form promoting itself
    over a row people already use (see the longest-wins regression its comment
    describes). Coalescing must not smuggle the second form into that slot.
    """
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph(
        [_entity("IBM", "organization"), _entity("I.B.M.", "organization")]
    )
    sc = _sc([_match(), _match()])
    mock_sc_factory.return_value = sc

    await _run()

    item = _items_for(sc, EXISTING_ID)[0]
    assert item["canonical_name"] == EXISTING_NAME
    assert item["action"] == "update"


@_worker_patches
async def test_the_coalesced_form_still_resolves_as_a_relation_endpoint(
    mock_resolve, mock_extract, mock_sc_factory, _embed, _log, mock_rel
):
    """Coalescing removes an ITEM, never a NAME.

    Both surface forms must still map to the row in ``name_to_id``, or a
    relation the extractor stated in terms of the second one silently loses its
    endpoint and the edge is never written — trading a lost alias for a lost
    edge.
    """
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph(
        [
            _entity("IBM", "organization"),
            _entity("I.B.M.", "organization"),
            _entity("watson", "product", "object"),
        ],
        [_relation("I.B.M.", "builds", "watson")],
    )
    sc = _build_sc_mock(
        resolve_returns=[_match(), _match(), None],
        upsert_returns=[
            {"input_idx": 0, "entity_id": EXISTING_ID, "action": "updated"},
            {"input_idx": 1, "entity_id": str(uuid4()), "action": "created"},
        ],
    )
    sc.set_subject_entity_if_null = AsyncMock(return_value=True)
    sc.set_predicate_if_null = AsyncMock(return_value=True)
    mock_sc_factory.return_value = sc
    mock_rel.return_value = [True]

    await _run()

    sent = mock_rel.await_args.args[0]
    assert len(sent) == 1, (
        "the relation stated via the coalesced form must still be sent"
    )
    assert str(sent[0].from_entity_id) == EXISTING_ID


@_worker_patches
async def test_input_idx_still_tiles_the_payload_contiguously(
    mock_resolve, mock_extract, mock_sc_factory, _embed, _log, _rel
):
    """Storage rejects a batch whose ``input_idx`` values are not exactly
    ``[0, len(items))`` — ``_validate_input_idxs`` answers 422. Dropping an
    item without renumbering is how a coalescing fix turns a lost alias into a
    lost batch."""
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph(
        [
            _entity("IBM", "organization"),
            _entity("I.B.M.", "organization"),
            _entity("watson", "product", "object"),
        ]
    )
    sc = _build_sc_mock(
        resolve_returns=[_match(), _match(), None],
        upsert_returns=[
            {"input_idx": 0, "entity_id": EXISTING_ID, "action": "updated"},
            {"input_idx": 1, "entity_id": str(uuid4()), "action": "created"},
        ],
    )
    sc.set_subject_entity_if_null = AsyncMock(return_value=True)
    sc.set_predicate_if_null = AsyncMock(return_value=True)
    mock_sc_factory.return_value = sc

    await _run()

    items = sc.bulk_upsert_entities.await_args.kwargs["items"]
    assert [i["input_idx"] for i in items] == list(range(len(items)))


@_worker_patches
async def test_unrelated_entities_are_not_coalesced(
    mock_resolve, mock_extract, mock_sc_factory, _embed, _log, _rel
):
    """Non-vacuity. Two forms resolving to DIFFERENT rows must stay two items —
    a fix that merged on name similarity rather than on the resolved
    ``entity_id`` would collapse them and lose a whole entity."""
    other_id = str(uuid4())
    mock_resolve.return_value = _config()
    mock_extract.return_value = _graph(
        [_entity("IBM", "organization"), _entity("I.B.M.", "organization")]
    )
    sc = _build_sc_mock(
        resolve_returns=[
            _match(),
            {
                "entity_id": other_id,
                "canonical_name": "I.B.M. Credit",
                "attributes": {},
                "matched_by": "exact",
                "similarity": 1.0,
            },
        ],
        upsert_returns=[
            {"input_idx": 0, "entity_id": EXISTING_ID, "action": "updated"},
            {"input_idx": 1, "entity_id": other_id, "action": "updated"},
        ],
    )
    sc.set_subject_entity_if_null = AsyncMock(return_value=True)
    sc.set_predicate_if_null = AsyncMock(return_value=True)
    mock_sc_factory.return_value = sc

    await _run()

    items = sc.bulk_upsert_entities.await_args.kwargs["items"]
    assert len(items) == 2
    assert {i["entity_id"] for i in items} == {EXISTING_ID, other_id}
