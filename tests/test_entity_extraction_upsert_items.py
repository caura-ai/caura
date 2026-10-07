"""What the extraction worker sends to, and takes from, the bulk upsert (L-46, L-29).

L-46: storage now merges an update item into the locked row, so the worker
sends only what this extraction adds, its aliases. A copy of the row's other
attributes from the resolve snapshot would write stale values back over a key
another writer changed in between.

L-29: a row the upsert reports ``missing`` was deleted between resolve and
upsert. The worker used to map its id like any other, so the memory was linked,
related and subject-written to an entity that does not exist, and the audit
counted it.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core_api.services import entity_extraction_worker as w
from core_api.services.entity_extraction import (
    ExtractedEntity,
    ExtractedGraph,
    ExtractedRelation,
)
from tests.conftest import close_scheduled_coro

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_CONFIG = SimpleNamespace(
    entity_blocklist=frozenset(), auto_entity_linking_enabled=False
)


def _sc(resolved: list, upserted: list) -> MagicMock:
    sc = MagicMock()
    sc.get_memory = AsyncMock(return_value={"id": "m", "deleted_at": None})
    sc.bulk_resolve_entities = AsyncMock(return_value=resolved)
    sc.bulk_upsert_entities = AsyncMock(return_value=upserted)
    sc.bulk_upsert_entity_links = AsyncMock(
        side_effect=lambda tenant_id, items: [
            {"input_idx": i["input_idx"], "created": True} for i in items
        ]
    )
    sc.set_subject_entity_if_null = AsyncMock(return_value=True)
    sc.set_predicate_if_null = AsyncMock(return_value=True)
    return sc


async def _run(sc: MagicMock, graph: ExtractedGraph) -> SimpleNamespace:
    seen = SimpleNamespace(log=AsyncMock(), relation=AsyncMock())
    with (
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=_CONFIG),
        ),
        patch.object(
            w, "extract_entities_from_content", new=AsyncMock(return_value=graph)
        ),
        patch.object(w, "get_storage_client", return_value=sc),
        patch.object(w, "get_embedding", new=AsyncMock(return_value=None)),
        patch.object(w, "log_action", new=seen.log),
        patch.object(w, "upsert_relation", new=seen.relation),
        patch("core_api.tasks.track_task", side_effect=close_scheduled_coro),
    ):
        await w.process_entity_extraction(uuid4(), "t1", None, "a1", "text", "fact")
    return seen


async def test_an_update_item_carries_only_the_aliases_it_adds():
    entity_id = str(uuid4())
    sc = _sc(
        resolved=[
            {
                "entity_id": entity_id,
                "canonical_name": "acme",
                "attributes": {"_aliases": ["acme", "acme corp"], "hq": "Berlin"},
                "matched_by": "similarity",
                "similarity": 0.91,
            }
        ],
        upserted=[{"input_idx": 0, "entity_id": entity_id, "action": "updated"}],
    )
    graph = ExtractedGraph(
        entities=[ExtractedEntity(canonical_name="acme co", entity_type="organization")]
    )

    await _run(sc, graph)

    [item] = sc.bulk_upsert_entities.call_args.kwargs["items"]
    assert item["action"] == "update"
    assert item["canonical_name"] == "acme"
    assert item["attributes"] == {"_aliases": ["acme", "acme co"]}


async def test_a_missing_upsert_row_is_left_out_of_the_memorys_graph():
    gone, kept = str(uuid4()), str(uuid4())
    sc = _sc(
        resolved=[None, None],
        upserted=[
            {"input_idx": 0, "entity_id": gone, "action": "missing"},
            {"input_idx": 1, "entity_id": kept, "action": "created"},
        ],
    )
    graph = ExtractedGraph(
        entities=[
            ExtractedEntity(
                canonical_name="alice morgan", entity_type="person", role="subject"
            ),
            ExtractedEntity(canonical_name="berlin office", entity_type="location"),
        ],
        relations=[
            ExtractedRelation(
                from_entity="alice morgan",
                relation_type="located_in",
                to_entity="berlin office",
            )
        ],
    )

    seen = await _run(sc, graph)

    links = sc.bulk_upsert_entity_links.call_args.kwargs["items"]
    assert [link["entity_id"] for link in links] == [kept]
    seen.relation.assert_not_awaited()
    sc.set_subject_entity_if_null.assert_not_awaited()
    assert seen.log.call_args.kwargs["detail"]["entities_count"] == 1
