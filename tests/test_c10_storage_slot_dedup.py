"""C10: per-tenant storage-slot deduplication across the search pipeline.

When ``ClassifyQuery``'s entity-lookup short-circuit runs ``_collect_memories``
it acquires ``per_tenant_storage_slot("storage_search", tenant_id)`` around
the ``storage_client.load_memories_by_ids(...)`` round-trip. If the entity
lookup falls through (matched entities, but their linked memories all got
filtered out), the plan still routes through ``ExecuteScoredSearch`` which
*also* used to acquire the same slot — charging the tenant's
``storage_search`` bucket TWICE for one logical search.

C10's fix: when classify acquires + releases the slot, it sets
``ctx.data["_storage_slot_acquired"] = True``. ``ExecuteScoredSearch`` reads
that key and SKIPS the slot acquisition (but still runs ``scored_search``).
Net effect: one logical search → one slot acquired against the bucket,
regardless of path.

These tests pin that contract without exercising the real semaphore — the
slot context manager is replaced by a recording CM on both modules'
import-bindings, and each test asserts the (module, key, tenant_id) tuples
that got recorded.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult
from core_api.pipeline.steps.search.classify_query import ClassifyQuery
from core_api.pipeline.steps.search.execute_scored_search import ExecuteScoredSearch
from core_api.pipeline.steps.search.retrieval_types import (
    RetrievalPlan,
    RetrievalStrategy,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# Search params that satisfy every downstream reader (classify + execute).
# ExecuteScoredSearch's storage call paths read freshness_floor, recency_*,
# etc.; we set them defensively so a real scored_search call wouldn't blow
# up if a test inadvertently let one through.
_DEFAULT_SEARCH_PARAMS_FULL = {
    "fts_weight": 0.3,
    "graph_max_hops": 2,
    "top_k": 10,
    "min_similarity": 0.0,
    "freshness_floor": 0.0,
    "freshness_decay_days": 30,
    "recency_weight": 0.0,
    "recency_decay_days": 30,
}


# ---------------------------------------------------------------------------
# Recording slot patch helpers
# ---------------------------------------------------------------------------


def _make_recorder(acquisitions: list[tuple[str, str, str]], module_label: str):
    """Return an async CM-factory that records (module, key, tenant_id) on
    every ``__aenter__`` and yields immediately (no blocking)."""

    @asynccontextmanager
    async def _ctx(key: str, tenant_id: str):
        acquisitions.append((module_label, key, tenant_id))
        yield

    return _ctx


def _patch_slots(acquisitions: list[tuple[str, str, str]]):
    """Patch ``per_tenant_storage_slot`` on BOTH step modules' import-bindings.

    Returns a tuple of (patcher_classify, patcher_execute). Caller is
    responsible for starting/stopping both — typically via the ``with``
    helper :func:`_recording_slots` below.
    """
    classify_recorder = _make_recorder(acquisitions, "classify_query")
    execute_recorder = _make_recorder(acquisitions, "execute_scored_search")
    p1 = patch(
        "core_api.pipeline.steps.search.classify_query.per_tenant_storage_slot",
        classify_recorder,
    )
    p2 = patch(
        "core_api.pipeline.steps.search.execute_scored_search.per_tenant_storage_slot",
        execute_recorder,
    )
    return p1, p2


class _recording_slots:
    """Context manager that installs the recording slot CMs on both modules."""

    def __init__(self):
        self.acquisitions: list[tuple[str, str, str]] = []
        self._p1 = None
        self._p2 = None

    def __enter__(self):
        self._p1, self._p2 = _patch_slots(self.acquisitions)
        self._p1.start()
        self._p2.start()
        return self.acquisitions

    def __exit__(self, exc_type, exc, tb):
        self._p2.stop()
        self._p1.stop()
        return False


# ---------------------------------------------------------------------------
# ctx + storage client builders
# ---------------------------------------------------------------------------


def _make_classify_ctx(
    query: str, *, tenant_id: str = "t1", **extra
) -> PipelineContext:
    """Build a PipelineContext with the keys ClassifyQuery reads."""
    data: dict[str, Any] = {
        "query": query,
        "tenant_id": tenant_id,
        "fleet_ids": ["fleet-1"],
        "search_params": dict(_DEFAULT_SEARCH_PARAMS_FULL),
        **extra,
    }
    return PipelineContext(data=data)


def _prime_for_execute(ctx: PipelineContext) -> None:
    """Populate ctx with the keys ExecuteScoredSearch reads.

    The real pipeline composition wires these from ParallelEmbedAndEntityBoost;
    in isolation we just stuff plausible values into ctx.data.
    """
    ctx.data.setdefault("embedding", [0.0] * 8)
    ctx.data.setdefault("temporal_window", None)
    ctx.data.setdefault("date_range_filter", None)
    ctx.data.setdefault("boosted_memory_ids", set())
    ctx.data.setdefault("memory_boost_factor", {})
    ctx.data.setdefault("tenant_config", None)
    ctx.data.setdefault("graph_expand", True)


def _entity_match_sc(
    *,
    eid: str,
    mid: str,
    memories: list[dict] | None,
    raise_on_load: Exception | None = None,
) -> AsyncMock:
    """Storage client mock that produces a hit through every entity-lookup
    gate: fts_search_entities → expand_graph → get_memory_ids_by_entity_ids
    → load_memories_by_ids.
    """
    sc = AsyncMock()
    sc.fts_search_entities = AsyncMock(return_value=[eid])
    sc.expand_graph = AsyncMock(return_value={eid: {"hop": 0, "weight": 1.0}})
    sc.get_memory_ids_by_entity_ids = AsyncMock(
        return_value=[{"memory_id": mid, "entity_id": eid, "role": "subject"}]
    )
    if raise_on_load is not None:
        sc.load_memories_by_ids = AsyncMock(side_effect=raise_on_load)
    else:
        sc.load_memories_by_ids = AsyncMock(return_value=memories or [])
    sc.scored_search = AsyncMock(return_value=[])
    return sc


def _bare_sc() -> AsyncMock:
    """Storage client mock for the non-entity-lookup path: fts_search_entities
    returns no matches, so classify never enters _collect_memories."""
    sc = AsyncMock()
    sc.fts_search_entities = AsyncMock(return_value=[])
    sc.expand_graph = AsyncMock(return_value={})
    sc.get_memory_ids_by_entity_ids = AsyncMock(return_value=[])
    sc.load_memories_by_ids = AsyncMock(return_value=[])
    sc.scored_search = AsyncMock(return_value=[])
    return sc


class _shared_storage_client:
    """Patch ``get_storage_client`` on BOTH step modules to return the same
    mock — otherwise ``ExecuteScoredSearch`` would resolve the storage client
    via the conftest autouse fixture's ASGI-bridged singleton and call the
    real storage app (which we don't want in a unit test)."""

    def __init__(self, sc):
        self.sc = sc
        self._p1 = patch(
            "core_api.pipeline.steps.search.classify_query.get_storage_client",
            return_value=sc,
        )
        self._p2 = patch(
            "core_api.pipeline.steps.search.execute_scored_search.get_storage_client",
            return_value=sc,
        )

    def __enter__(self):
        self._p1.start()
        self._p2.start()
        return self.sc

    def __exit__(self, exc_type, exc, tb):
        self._p2.stop()
        self._p1.stop()
        return False


# ---------------------------------------------------------------------------
# Case 1 — entity-lookup SUCCESS → only classify acquires, execute skips
# ---------------------------------------------------------------------------


async def test_entity_lookup_success_classify_acquires_once_execute_skips():
    eid = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    sc = _entity_match_sc(
        eid=eid,
        mid=mid,
        memories=[
            {
                "id": mid,
                "tenant_id": "t1",
                "content": "Alice test memory",
                "memory_type": "fact",
            }
        ],
    )
    ctx = _make_classify_ctx("Alice")

    with _shared_storage_client(sc), _recording_slots() as acquisitions:
        await ClassifyQuery().execute(ctx)

        assert acquisitions == [("classify_query", "storage_search", "t1")]
        plan: RetrievalPlan = ctx.data["retrieval_plan"]
        assert plan.strategy == RetrievalStrategy.ENTITY_LOOKUP
        assert plan.skip_scored_search is True
        assert ctx.data.get("_storage_slot_acquired") is True
        assert len(ctx.data.get("filtered_rows", [])) > 0

        # ExecuteScoredSearch must SKIP — no slot, no scored_search call.
        _prime_for_execute(ctx)
        result = await ExecuteScoredSearch().execute(ctx)

        assert isinstance(result, StepResult)
        assert result.outcome == StepOutcome.SKIPPED
        sc.scored_search.assert_not_awaited()
        # Still exactly one acquisition recorded.
        assert acquisitions == [("classify_query", "storage_search", "t1")]


# ---------------------------------------------------------------------------
# Case 2 — entity-lookup FALL-THROUGH → classify acquires, execute does not
# re-acquire but still calls scored_search
# ---------------------------------------------------------------------------


async def test_entity_lookup_fallthrough_execute_skips_slot_but_calls_scored_search():
    eid = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    # load_memories_by_ids returns []: _collect_memories runs the load, marks
    # the sentinel, then returns [] → classify falls through past the
    # ENTITY_LOOKUP plan-emission.
    sc = _entity_match_sc(eid=eid, mid=mid, memories=[])
    ctx = _make_classify_ctx("Alice")

    with _shared_storage_client(sc), _recording_slots() as acquisitions:
        await ClassifyQuery().execute(ctx)

        # Classify acquired once during the load.
        assert acquisitions == [("classify_query", "storage_search", "t1")]
        plan: RetrievalPlan = ctx.data["retrieval_plan"]
        assert plan.strategy != RetrievalStrategy.ENTITY_LOOKUP
        assert plan.skip_scored_search is False
        # Sentinel is set even though no entity-lookup rows were emitted.
        assert ctx.data.get("_storage_slot_acquired") is True
        sc.load_memories_by_ids.assert_awaited_once()

        # Now ExecuteScoredSearch must run scored_search WITHOUT re-acquiring.
        _prime_for_execute(ctx)
        sc.scored_search.reset_mock()
        sc.scored_search.return_value = []
        await ExecuteScoredSearch().execute(ctx)

        # Still exactly one acquisition — execute did NOT re-take the slot.
        assert acquisitions == [("classify_query", "storage_search", "t1")]
        # But scored_search WAS called (the storage call still happens).
        sc.scored_search.assert_awaited_once()


# ---------------------------------------------------------------------------
# Case 3 — NON entity-lookup path → execute acquires normally
# ---------------------------------------------------------------------------


async def test_non_entity_lookup_path_execute_acquires_normally():
    sc = _bare_sc()
    # A query with no entity tokens — classify routes via SEMANTIC_SEARCH
    # without ever running _collect_memories.
    ctx = _make_classify_ctx("what do we know about pricing strategy next quarter")

    with _shared_storage_client(sc), _recording_slots() as acquisitions:
        await ClassifyQuery().execute(ctx)

        assert acquisitions == []
        assert "_storage_slot_acquired" not in ctx.data
        plan: RetrievalPlan = ctx.data["retrieval_plan"]
        assert plan.skip_scored_search is False
        sc.load_memories_by_ids.assert_not_awaited()

        _prime_for_execute(ctx)
        sc.scored_search.return_value = []
        await ExecuteScoredSearch().execute(ctx)

        # Execute acquired the slot because classify did not mark the sentinel.
        assert acquisitions == [("execute_scored_search", "storage_search", "t1")]
        sc.scored_search.assert_awaited_once()


# ---------------------------------------------------------------------------
# Case 4 — sentinel suppresses ONLY the slot, NOT the storage call
# ---------------------------------------------------------------------------


async def test_sentinel_only_suppresses_slot_not_scored_search_call():
    eid = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    sc = _entity_match_sc(eid=eid, mid=mid, memories=[])
    ctx = _make_classify_ctx("Alice")

    with _shared_storage_client(sc), _recording_slots() as acquisitions:
        await ClassifyQuery().execute(ctx)
        assert ctx.data.get("_storage_slot_acquired") is True
        assert len(acquisitions) == 1

        _prime_for_execute(ctx)
        # Sentinel set, but scored_search MUST still be called with the
        # expected tenant_id baked into its search_data argument.
        sc.scored_search.reset_mock()
        sc.scored_search.return_value = []
        await ExecuteScoredSearch().execute(ctx)

        sc.scored_search.assert_awaited_once()
        call_args = sc.scored_search.await_args
        # scored_search's first positional arg is the search_data dict in
        # current usage; also accept tenant_id passed as kwarg.
        search_data = call_args.args[0] if call_args.args else call_args.kwargs
        assert search_data.get("tenant_id") == "t1"


# ---------------------------------------------------------------------------
# Case 5 — sentinel does NOT leak across requests
# ---------------------------------------------------------------------------


async def test_sentinel_does_not_leak_across_requests():
    # First request: entity-lookup fall-through → sentinel set, execute skips
    # the slot.
    eid = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    sc_first = _entity_match_sc(eid=eid, mid=mid, memories=[])
    ctx_a = _make_classify_ctx("Alice")

    with _shared_storage_client(sc_first) as _sc, _recording_slots() as acquisitions:
        await ClassifyQuery().execute(ctx_a)
        _prime_for_execute(ctx_a)
        sc_first.scored_search.return_value = []
        await ExecuteScoredSearch().execute(ctx_a)

        # First request: exactly 1 acquisition (classify only).
        assert acquisitions == [("classify_query", "storage_search", "t1")]

    # Second request: non-entity-lookup path. Fresh ctx → fresh
    # ``_storage_slot_acquired`` state. Execute must acquire the slot.
    sc_second = _bare_sc()
    ctx_b = _make_classify_ctx("what do we know about pricing strategy next quarter")

    with _shared_storage_client(sc_second), _recording_slots() as acquisitions_b:
        await ClassifyQuery().execute(ctx_b)
        assert "_storage_slot_acquired" not in ctx_b.data

        _prime_for_execute(ctx_b)
        sc_second.scored_search.return_value = []
        await ExecuteScoredSearch().execute(ctx_b)

        # The second request, in isolation, recorded one execute-side slot.
        assert acquisitions_b == [("execute_scored_search", "storage_search", "t1")]


# ---------------------------------------------------------------------------
# Case 6 — load_memories_by_ids failure does NOT mark the sentinel
# ---------------------------------------------------------------------------


async def test_load_failure_does_not_mark_sentinel():
    eid = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    boom = RuntimeError("storage exploded")
    sc = _entity_match_sc(eid=eid, mid=mid, memories=None, raise_on_load=boom)
    ctx = _make_classify_ctx("Alice")

    with _shared_storage_client(sc), _recording_slots() as acquisitions:
        # Classify's outer try/except may catch the exception and fall through.
        # If it doesn't, the test should still surface the failure cleanly.
        try:
            await ClassifyQuery().execute(ctx)
        except RuntimeError as exc:
            assert exc is boom

        # Sentinel must NOT be set — the implementation marks it only after
        # a successful load.
        assert ctx.data.get("_storage_slot_acquired") is not True

        # Classify entered the slot (the ``async with`` records on enter)
        # even though the load inside raised. That's expected: the slot is
        # released on exception by ``async with``'s __aexit__.
        assert acquisitions == [("classify_query", "storage_search", "t1")]

        # If classify swallowed the exception and set a retrieval_plan, the
        # downstream ExecuteScoredSearch must acquire the slot itself (since
        # the sentinel was never set). If classify propagated the exception,
        # ExecuteScoredSearch wouldn't run in the real pipeline — but we
        # exercise it here to pin the sentinel-not-set behaviour.
        if (
            "retrieval_plan" in ctx.data
            and not ctx.data["retrieval_plan"].skip_scored_search
        ):
            _prime_for_execute(ctx)
            sc.scored_search.reset_mock()
            sc.scored_search.return_value = []
            await ExecuteScoredSearch().execute(ctx)
            assert acquisitions == [
                ("classify_query", "storage_search", "t1"),
                ("execute_scored_search", "storage_search", "t1"),
            ]
            sc.scored_search.assert_awaited_once()


# ---------------------------------------------------------------------------
# Case 7 — explicit skip path: when classify already emitted a
# skip_scored_search plan (entity-lookup SUCCESS), ExecuteScoredSearch must
# NOT touch scored_search or the slot regardless of the sentinel value.
# ---------------------------------------------------------------------------


async def test_execute_skips_when_plan_says_skip_regardless_of_sentinel():
    ctx = PipelineContext(
                data={
            "tenant_id": "t1",
            "search_params": dict(_DEFAULT_SEARCH_PARAMS_FULL),
            "retrieval_plan": RetrievalPlan(
                strategy=RetrievalStrategy.ENTITY_LOOKUP,
                skip_scored_search=True,
            ),
            # Sentinel set, just like a real entity-lookup SUCCESS path.
            "_storage_slot_acquired": True,
        },
    )
    _prime_for_execute(ctx)

    with _recording_slots() as acquisitions:
        result = await ExecuteScoredSearch().execute(ctx)
        assert isinstance(result, StepResult)
        assert result.outcome == StepOutcome.SKIPPED
        assert acquisitions == []
