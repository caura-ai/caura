"""CAURA-684: ENTITY_LOOKUP short-circuit shape contract.

Before this fix, ``ClassifyQuery._expand_per_fleet`` indexed the
``expand_graph`` response positionally (``hop_weight[0]`` /
``hop_weight[1]``), but storage returns ``{"hop": int, "weight": float}``
dicts — see ``core-storage-api/.../routers/entities.py`` ``expand_graph``
route. Every entity-token query KeyError'd at line 214, the broad
``except Exception:`` at line 123 swallowed it, and the pipeline fell
through to keyword/semantic search.

Confirmed in prod logs before fix:
  staging-memclaw-core-api : 1,327 occurrences / 24h
  prod-memclaw-core-api    : 206 occurrences / 24h

This test pins the dict-shape contract. It mocks storage to return the
shape the real ``expand_graph`` route emits and asserts ``ClassifyQuery``
selects the ENTITY_LOOKUP strategy end-to-end. ``scored_search`` is
mocked to return memory rows directly, isolating this test from the
separate ``_collect_memories`` payload-contract bug (omitted
``embedding``/``query``/``search_params``) that is tracked as a follow-up.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.unit


async def test_entity_lookup_short_circuit_fires_with_dict_shaped_expand_graph():
    """ClassifyQuery selects ENTITY_LOOKUP when entity FTS + expand_graph
    produce matches and ``_collect_memories`` returns rows.

    The critical fixture detail is ``expand_graph`` returning
    ``{eid_str: {"hop": 0, "weight": 1.0}}`` — the dict shape the real
    storage route emits. The pre-fix consumer at classify_query.py:214
    raised ``KeyError: 0`` against this shape.
    """
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.classify_query import ClassifyQuery
    from core_api.pipeline.steps.search.retrieval_types import RetrievalStrategy

    matched_entity_id = uuid4()
    matched_memory_id = str(uuid4())

    fake_storage = AsyncMock()
    fake_storage.fts_search_entities = AsyncMock(return_value=[str(matched_entity_id)])
    # Storage shape: dict with "hop" / "weight" keys. Matches
    # core-storage-api/.../routers/entities.py expand_graph route.
    fake_storage.expand_graph = AsyncMock(
        return_value={str(matched_entity_id): {"hop": 0, "weight": 1.0}}
    )
    fake_storage.get_memory_ids_by_entity_ids = AsyncMock(
        return_value=[
            {
                "memory_id": matched_memory_id,
                "entity_id": str(matched_entity_id),
                "role": "subject",
            }
        ]
    )
    # CAURA-687: _collect_memories now calls the dedicated
    # load_memories_by_ids endpoint instead of scored_search.
    fake_storage.load_memories_by_ids = AsyncMock(
        return_value=[
            {
                "id": matched_memory_id,
                "tenant_id": "tenant-test",
                "content": "Heartbeat check completed",
                "memory_type": "task",
                "weight": 0.5,
                "status": "active",
                "ts_valid_start": None,
                "ts_valid_end": None,
                "metadata_": {},
                "fleet_id": None,
            }
        ]
    )

    ctx = PipelineContext()
    ctx.data = {
        "query": "central control heartbeat",
        "tenant_id": "tenant-test",
        "search_params": {
            "fts_weight": 0.3,
            "graph_max_hops": 2,
            "top_k": 5,
        },
        "temporal_window": None,
    }

    with patch(
        "core_api.pipeline.steps.search.classify_query.get_storage_client",
        return_value=fake_storage,
    ):
        await ClassifyQuery().execute(ctx)

    plan = ctx.data.get("retrieval_plan")
    assert plan is not None, "ClassifyQuery should always emit a retrieval_plan"
    assert plan.strategy == RetrievalStrategy.ENTITY_LOOKUP, (
        f"Expected ENTITY_LOOKUP short-circuit; got {plan.strategy}. "
        "If this fails with strategy=keyword/semantic, the broad except at "
        "classify_query.py:123 is hiding a regression — check the logs."
    )
    assert plan.skip_embedding is True
    assert plan.skip_scored_search is True
    assert str(matched_entity_id) in [str(eid) for eid in plan.matched_entity_ids]

    rows = ctx.data.get("filtered_rows", [])
    assert rows, "ENTITY_LOOKUP should populate filtered_rows for downstream steps"
    assert rows[0].Memory.id == matched_memory_id


async def test_expand_per_fleet_parses_dict_shape_directly():
    """Unit-level guard on the exact line that broke: ``_expand_per_fleet``
    must accept ``{eid_str: {"hop": int, "weight": float}}`` and return
    a merged ``{UUID: (hop, weight)}`` mapping.
    """
    from core_api.pipeline.steps.search.classify_query import ClassifyQuery

    eid1, eid2 = uuid4(), uuid4()
    fake_storage = AsyncMock()
    fake_storage.expand_graph = AsyncMock(
        return_value={
            str(eid1): {"hop": 0, "weight": 1.0},
            str(eid2): {"hop": 1, "weight": 0.8},
        }
    )

    merged = await ClassifyQuery._expand_per_fleet(
        fake_storage,
        seed_ids=[eid1],
        tenant_id="tenant-test",
        fleet_ids=None,
        max_hops=2,
        use_union=True,
    )

    assert merged == {eid1: (0, 1.0), eid2: (1, 0.8)}, (
        f"Expected merged hop/weight tuples; got {merged}"
    )


async def test_collect_memories_forwards_valid_at_and_readable_tenant_ids():
    """CAURA-687: _collect_memories must forward valid_at and
    readable_tenant_ids to the load-by-ids endpoint. Pre-687 these were
    dropped — the short-circuit's visibility behaviour silently diverged
    from the scored-search fallthrough's (cross-tenant read widening and
    historical-question filtering both broken).
    """
    from datetime import datetime, timezone

    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.classify_query import ClassifyQuery
    from core_api.pipeline.steps.search.retrieval_types import RetrievalStrategy

    matched_entity_id = uuid4()
    matched_memory_id = str(uuid4())
    valid_at_dt = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)

    fake_storage = AsyncMock()
    fake_storage.fts_search_entities = AsyncMock(return_value=[str(matched_entity_id)])
    fake_storage.expand_graph = AsyncMock(
        return_value={str(matched_entity_id): {"hop": 0, "weight": 1.0}}
    )
    fake_storage.get_memory_ids_by_entity_ids = AsyncMock(
        return_value=[
            {"memory_id": matched_memory_id, "entity_id": str(matched_entity_id), "role": "subject"}
        ]
    )
    fake_storage.load_memories_by_ids = AsyncMock(
        return_value=[
            {
                "id": matched_memory_id,
                "tenant_id": "home-tenant",
                "content": "x",
                "memory_type": "task",
                "weight": 0.5,
                "status": "active",
                "ts_valid_start": None,
                "ts_valid_end": None,
                "metadata_": {},
                "fleet_id": None,
            }
        ]
    )

    ctx = PipelineContext()
    ctx.data = {
        "query": "central control",
        "tenant_id": "home-tenant",
        "search_params": {"fts_weight": 0.3, "graph_max_hops": 2, "top_k": 5},
        "temporal_window": None,
        "valid_at": valid_at_dt,
        "readable_tenant_ids": ["home-tenant", "source-tenant-a"],
    }

    with patch(
        "core_api.pipeline.steps.search.classify_query.get_storage_client",
        return_value=fake_storage,
    ):
        await ClassifyQuery().execute(ctx)

    assert ctx.data["retrieval_plan"].strategy == RetrievalStrategy.ENTITY_LOOKUP
    fake_storage.load_memories_by_ids.assert_awaited_once()
    sent = fake_storage.load_memories_by_ids.await_args.args[0]
    assert sent["valid_at"] == str(valid_at_dt), (
        f"valid_at must be forwarded as string; got {sent.get('valid_at')!r}"
    )
    assert sent["readable_tenant_ids"] == ["home-tenant", "source-tenant-a"], (
        f"readable_tenant_ids must be forwarded for cross-tenant reads; "
        f"got {sent.get('readable_tenant_ids')!r}"
    )
    # Sanity: the old stale keys (embedding/query/search_params/entity_lookup)
    # MUST NOT be present — they were the original payload-contract bug.
    assert "embedding" not in sent
    assert "query" not in sent
    assert "search_params" not in sent
    assert "entity_lookup" not in sent


async def test_collect_memories_omits_readable_tenant_ids_when_single_tenant():
    """Single-tenant callers (the common case) leave readable_tenant_ids
    absent from the payload so storage uses the cheap single-tenant
    WHERE predicate. Mirrors execute_scored_search.py:79-81.
    """
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.classify_query import ClassifyQuery

    matched_entity_id = uuid4()
    matched_memory_id = str(uuid4())

    fake_storage = AsyncMock()
    fake_storage.fts_search_entities = AsyncMock(return_value=[str(matched_entity_id)])
    fake_storage.expand_graph = AsyncMock(
        return_value={str(matched_entity_id): {"hop": 0, "weight": 1.0}}
    )
    fake_storage.get_memory_ids_by_entity_ids = AsyncMock(
        return_value=[
            {"memory_id": matched_memory_id, "entity_id": str(matched_entity_id), "role": "subject"}
        ]
    )
    fake_storage.load_memories_by_ids = AsyncMock(
        return_value=[
            {
                "id": matched_memory_id,
                "tenant_id": "home-tenant",
                "content": "x",
                "memory_type": "task",
                "weight": 0.5,
                "status": "active",
                "ts_valid_start": None,
                "ts_valid_end": None,
                "metadata_": {},
                "fleet_id": None,
            }
        ]
    )

    ctx = PipelineContext()
    ctx.data = {
        "query": "central control",
        "tenant_id": "home-tenant",
        "search_params": {"fts_weight": 0.3, "graph_max_hops": 2, "top_k": 5},
        "temporal_window": None,
        # Single-tenant: readable_tenant_ids unset OR equals [tenant_id]
        "readable_tenant_ids": ["home-tenant"],
    }

    with patch(
        "core_api.pipeline.steps.search.classify_query.get_storage_client",
        return_value=fake_storage,
    ):
        await ClassifyQuery().execute(ctx)

    sent = fake_storage.load_memories_by_ids.await_args.args[0]
    assert "readable_tenant_ids" not in sent, (
        "Single-tenant calls should not send readable_tenant_ids — "
        "storage falls back to the cheaper tenant_id == X predicate."
    )


async def test_collect_memories_forwards_single_element_when_different_from_home():
    """Edge case (PR 237 review): a single-element ``readable_tenant_ids``
    that names a tenant OTHER than ``tenant_id`` must still be forwarded.

    Previous guard ``len(readable_tenant_ids) > 1`` silently dropped this
    case, degrading to home-tenant-only reads with no error or log if the
    upstream middleware ever broke its "single element always == home"
    invariant. The replacement guard ``!= [tenant_id]`` handles it.
    """
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.classify_query import ClassifyQuery

    matched_entity_id = uuid4()
    matched_memory_id = str(uuid4())

    fake_storage = AsyncMock()
    fake_storage.fts_search_entities = AsyncMock(return_value=[str(matched_entity_id)])
    fake_storage.expand_graph = AsyncMock(
        return_value={str(matched_entity_id): {"hop": 0, "weight": 1.0}}
    )
    fake_storage.get_memory_ids_by_entity_ids = AsyncMock(
        return_value=[
            {"memory_id": matched_memory_id, "entity_id": str(matched_entity_id), "role": "subject"}
        ]
    )
    fake_storage.load_memories_by_ids = AsyncMock(
        return_value=[
            {
                "id": matched_memory_id,
                "tenant_id": "source-tenant",
                "content": "x",
                "memory_type": "task",
                "weight": 0.5,
                "status": "active",
                "ts_valid_start": None,
                "ts_valid_end": None,
                "metadata_": {},
                "fleet_id": None,
            }
        ]
    )

    ctx = PipelineContext()
    ctx.data = {
        "query": "central control",
        "tenant_id": "home-tenant",
        "search_params": {"fts_weight": 0.3, "graph_max_hops": 2, "top_k": 5},
        "temporal_window": None,
        # Single element, but NOT the home tenant — must still be forwarded.
        "readable_tenant_ids": ["source-tenant"],
    }

    with patch(
        "core_api.pipeline.steps.search.classify_query.get_storage_client",
        return_value=fake_storage,
    ):
        await ClassifyQuery().execute(ctx)

    sent = fake_storage.load_memories_by_ids.await_args.args[0]
    assert sent.get("readable_tenant_ids") == ["source-tenant"], (
        f"single-element readable_tenant_ids that differs from tenant_id "
        f"must be forwarded; got {sent.get('readable_tenant_ids')!r}"
    )
