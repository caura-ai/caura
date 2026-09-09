"""Per-tenant storage_search slot coverage across the search pipeline paths.

History, because this file used to pin the opposite contract. C10 (PR #264)
made ``ClassifyQuery``'s entity-lookup load set
``ctx.data["_storage_slot_acquired"] = True`` so that ``ExecuteScoredSearch``,
on the entity-lookup fall-through, SKIPPED acquiring
``per_tenant_storage_slot("storage_search", ...)`` — framed as "don't charge
the tenant twice for one logical search". That framing treated the slot as a
rate-limit charge. It is not: ``per_tenant_storage_slot`` is an in-flight cap
held only across a single storage roundtrip, and classify's slot is released
when its ``load_memories_by_ids`` roundtrip returns — before
``ExecuteScoredSearch`` ever runs. The two roundtrips are sequential, so
acquiring around each holds at most ONE slot at any instant; there was never a
double-charge to dedup. What the skip actually did was exempt the
fall-through's ``scored_search`` roundtrip from the cap entirely, letting a
tenant whose queries matched entity tokens but fell through park unbounded
concurrent scored_search calls on the storage-reader pool (audit
oss-0814-l-06).

The contract pinned now: EVERY storage roundtrip on the search path runs
inside its own ``storage_search`` slot, whatever route classification took —
and a logical search still never holds two slots at once, because the
roundtrips are sequential, not because one of them is exempt.

These tests do not exercise the real semaphore — the slot context manager is
replaced on both modules' import-bindings by a recorder that logs every
acquisition AND tracks live holds, so tests can assert the slot was held
WHILE the storage call ran (entering-then-releasing before the call would
also satisfy a bare acquisition log).
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


class _SlotRecorder:
    """Stand-in for ``per_tenant_storage_slot`` on both step modules.

    Beyond logging ``(module, key, tenant_id)`` per acquisition, it tracks
    live holds: ``held`` is the number of slots currently inside their
    ``async with``, ``max_held`` the high-water mark. A storage-client mock
    can capture ``held`` at call time to prove the roundtrip ran INSIDE the
    slot — the property oss-0814-l-06 found missing on the entity-lookup
    fall-through — and ``max_held == 1`` proves the sequential roundtrips
    never overlap holds (the double-charge C10 wrongly guarded against).
    """

    def __init__(self) -> None:
        self.acquisitions: list[tuple[str, str, str]] = []
        self.held = 0
        self.max_held = 0

    def bind(self, module_label: str):
        @asynccontextmanager
        async def _ctx(key: str, tenant_id: str):
            self.acquisitions.append((module_label, key, tenant_id))
            self.held += 1
            self.max_held = max(self.max_held, self.held)
            try:
                yield
            finally:
                self.held -= 1

        return _ctx


class _recording_slots:
    """Install one shared ``_SlotRecorder`` on BOTH step modules'
    import-bindings; yields the recorder."""

    def __init__(self):
        self.recorder = _SlotRecorder()
        self._p1 = patch(
            "core_api.pipeline.steps.search.classify_query.per_tenant_storage_slot",
            self.recorder.bind("classify_query"),
        )
        self._p2 = patch(
            "core_api.pipeline.steps.search.execute_scored_search.per_tenant_storage_slot",
            self.recorder.bind("execute_scored_search"),
        )

    def __enter__(self) -> _SlotRecorder:
        self._p1.start()
        self._p2.start()
        return self.recorder

    def __exit__(self, exc_type, exc, tb):
        self._p2.stop()
        self._p1.stop()
        return False


# ---------------------------------------------------------------------------
# ctx + storage client builders
# ---------------------------------------------------------------------------


def _make_classify_ctx(
    query: str, *, tenant_id: str = "t1", top_k: int | None = None, **extra
) -> PipelineContext:
    """Build a PipelineContext with the keys ClassifyQuery reads.

    ``top_k`` is a seam because H-03 made _collect_memories bail BEFORE the
    load when the linked-memory pool cannot fill it. ``_entity_match_sc``
    links exactly one memory, so tests here that need the load to actually
    happen — every fall-through test does — must ask for one row. Otherwise
    no load runs and the fall-through under test never spent a slot.
    """
    search_params = dict(_DEFAULT_SEARCH_PARAMS_FULL)
    if top_k is not None:
        search_params["top_k"] = top_k
    data: dict[str, Any] = {
        "query": query,
        "tenant_id": tenant_id,
        "fleet_ids": ["fleet-1"],
        "search_params": search_params,
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
    recorder: _SlotRecorder | None = None,
) -> AsyncMock:
    """Storage client mock that produces a hit through every entity-lookup
    gate: fts_search_entities → expand_graph → get_memory_ids_by_entity_ids
    → load_memories_by_ids.

    When ``recorder`` is given, ``scored_search`` captures ``recorder.held``
    at call time into ``sc.scored_search_held_at_call`` so tests can assert
    the roundtrip ran inside the slot, not merely after an acquire/release.
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
    _wire_scored_search(sc, recorder)
    return sc


def _bare_sc(recorder: _SlotRecorder | None = None) -> AsyncMock:
    """Storage client mock for the non-entity-lookup path: fts_search_entities
    returns no matches, so classify never enters _collect_memories."""
    sc = AsyncMock()
    sc.fts_search_entities = AsyncMock(return_value=[])
    sc.expand_graph = AsyncMock(return_value={})
    sc.get_memory_ids_by_entity_ids = AsyncMock(return_value=[])
    sc.load_memories_by_ids = AsyncMock(return_value=[])
    _wire_scored_search(sc, recorder)
    return sc


def _wire_scored_search(sc: AsyncMock, recorder: _SlotRecorder | None) -> None:
    sc.scored_search_held_at_call = []
    if recorder is None:
        sc.scored_search = AsyncMock(return_value=[])
        return

    async def _scored_search(_search_data: dict) -> list:
        sc.scored_search_held_at_call.append(recorder.held)
        return []

    sc.scored_search = AsyncMock(side_effect=_scored_search)


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
# Case 1 — entity-lookup SUCCESS → classify acquires once; execute is
# plan-skipped entirely (no slot, no storage call)
# ---------------------------------------------------------------------------


async def test_entity_lookup_success_classify_acquires_once_execute_skips():
    eid = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    with _recording_slots() as rec:
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
            recorder=rec,
        )
        ctx = _make_classify_ctx("Alice", top_k=1)

        with _shared_storage_client(sc):
            await ClassifyQuery().execute(ctx)

            assert rec.acquisitions == [("classify_query", "storage_search", "t1")]
            plan: RetrievalPlan = ctx.data["retrieval_plan"]
            assert plan.strategy == RetrievalStrategy.ENTITY_LOOKUP
            assert plan.skip_scored_search is True
            assert len(ctx.data.get("filtered_rows", [])) > 0

            # ExecuteScoredSearch must SKIP — no slot, no scored_search call.
            _prime_for_execute(ctx)
            result = await ExecuteScoredSearch().execute(ctx)

            assert isinstance(result, StepResult)
            assert result.outcome == StepOutcome.SKIPPED
            sc.scored_search.assert_not_awaited()
            # Still exactly one acquisition recorded.
            assert rec.acquisitions == [("classify_query", "storage_search", "t1")]


# ---------------------------------------------------------------------------
# Case 2 — entity-lookup FALL-THROUGH → scored_search runs INSIDE its own
# slot (oss-0814-l-06: this roundtrip used to run outside the cap)
# ---------------------------------------------------------------------------


async def test_entity_lookup_fallthrough_scored_search_runs_inside_the_slot():
    eid = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    with _recording_slots() as rec:
        # load_memories_by_ids returns []: the pool looked adequate, the load
        # ran (spending classify's slot), then visibility filtering dropped
        # every row → classify falls through past the ENTITY_LOOKUP
        # plan-emission. This is the exact path the audit flagged.
        sc = _entity_match_sc(eid=eid, mid=mid, memories=[], recorder=rec)
        ctx = _make_classify_ctx("Alice", top_k=1)

        with _shared_storage_client(sc):
            await ClassifyQuery().execute(ctx)

            # Classify acquired once, around the load, and released it.
            assert rec.acquisitions == [("classify_query", "storage_search", "t1")]
            assert rec.held == 0
            plan: RetrievalPlan = ctx.data["retrieval_plan"]
            assert plan.strategy != RetrievalStrategy.ENTITY_LOOKUP
            assert plan.skip_scored_search is False
            sc.load_memories_by_ids.assert_awaited_once()

            _prime_for_execute(ctx)
            await ExecuteScoredSearch().execute(ctx)

            # Execute took its own slot for its own roundtrip.
            assert rec.acquisitions == [
                ("classify_query", "storage_search", "t1"),
                ("execute_scored_search", "storage_search", "t1"),
            ]
            # And the roundtrip ran INSIDE it — held == 1 at call time, not 0
            # (which is what the retired C10 skip produced here).
            sc.scored_search.assert_awaited_once()
            assert sc.scored_search_held_at_call == [1]
            # Sequential roundtrips never overlap holds: one logical search
            # occupies at most one slot at any instant, so there was no
            # double-charge for the skip to dedup in the first place.
            assert rec.max_held == 1
            assert rec.held == 0


# ---------------------------------------------------------------------------
# Case 3 — NON entity-lookup path → execute acquires normally
# ---------------------------------------------------------------------------


async def test_non_entity_lookup_path_execute_acquires_normally():
    with _recording_slots() as rec:
        sc = _bare_sc(recorder=rec)
        # A query with no entity tokens — classify routes via SEMANTIC_SEARCH
        # without ever running _collect_memories.
        ctx = _make_classify_ctx("what do we know about pricing strategy next quarter")

        with _shared_storage_client(sc):
            await ClassifyQuery().execute(ctx)

            assert rec.acquisitions == []
            plan: RetrievalPlan = ctx.data["retrieval_plan"]
            assert plan.skip_scored_search is False
            sc.load_memories_by_ids.assert_not_awaited()

            _prime_for_execute(ctx)
            await ExecuteScoredSearch().execute(ctx)

            assert rec.acquisitions == [
                ("execute_scored_search", "storage_search", "t1")
            ]
            sc.scored_search.assert_awaited_once()
            assert sc.scored_search_held_at_call == [1]


# ---------------------------------------------------------------------------
# Case 4 — a stale C10 sentinel must be inert: nothing on ctx may disable
# the bulkhead
# ---------------------------------------------------------------------------


async def test_stale_sentinel_does_not_disable_the_bulkhead():
    """Regression guard for reintroducing the skip. ``_storage_slot_acquired``
    is written by nothing anymore, but a ctx replayed from an old caller (or a
    hand-built test ctx — tests/test_audit_s6_c1_events.py used to do exactly
    this to dodge the slot) could still carry it; execute must acquire
    regardless."""
    with _recording_slots() as rec:
        ctx = _make_classify_ctx("Alice", top_k=1)
        ctx.data["_storage_slot_acquired"] = True
        _prime_for_execute(ctx)
        sc = _bare_sc(recorder=rec)

        with _shared_storage_client(sc):
            await ExecuteScoredSearch().execute(ctx)

        assert rec.acquisitions == [("execute_scored_search", "storage_search", "t1")]
        assert sc.scored_search_held_at_call == [1]


# ---------------------------------------------------------------------------
# Case 5 — load_memories_by_ids failure: classify's slot is released by the
# ``async with`` on the exception; the fall-through search still gets gated
# ---------------------------------------------------------------------------


async def test_load_failure_releases_slot_and_fallthrough_still_gated():
    eid = str(uuid.uuid4())
    mid = str(uuid.uuid4())
    boom = RuntimeError("storage exploded")
    with _recording_slots() as rec:
        sc = _entity_match_sc(
            eid=eid, mid=mid, memories=None, raise_on_load=boom, recorder=rec
        )
        ctx = _make_classify_ctx("Alice", top_k=1)

        with _shared_storage_client(sc):
            # Classify's outer try/except catches the load failure and falls
            # through to the keyword/semantic cascade.
            await ClassifyQuery().execute(ctx)

            # Classify entered the slot (recorded on enter) even though the
            # load inside raised; ``async with``'s __aexit__ released it.
            assert rec.acquisitions == [("classify_query", "storage_search", "t1")]
            assert rec.held == 0

            plan: RetrievalPlan = ctx.data["retrieval_plan"]
            assert plan.skip_scored_search is False

            _prime_for_execute(ctx)
            await ExecuteScoredSearch().execute(ctx)
            assert rec.acquisitions == [
                ("classify_query", "storage_search", "t1"),
                ("execute_scored_search", "storage_search", "t1"),
            ]
            sc.scored_search.assert_awaited_once()
            assert sc.scored_search_held_at_call == [1]


# ---------------------------------------------------------------------------
# Case 6 — explicit skip path: when classify already emitted a
# skip_scored_search plan (entity-lookup SUCCESS), ExecuteScoredSearch must
# NOT touch scored_search or the slot — the skip is the PLAN's, decided
# before the slot block, never a sentinel's.
# ---------------------------------------------------------------------------


async def test_execute_skips_when_plan_says_skip():
    ctx = PipelineContext(
        data={
            "tenant_id": "t1",
            "search_params": dict(_DEFAULT_SEARCH_PARAMS_FULL),
            "retrieval_plan": RetrievalPlan(
                strategy=RetrievalStrategy.ENTITY_LOOKUP,
                skip_scored_search=True,
            ),
            # A stale sentinel must be inert on this path too.
            "_storage_slot_acquired": True,
        },
    )
    _prime_for_execute(ctx)

    with _recording_slots() as rec:
        result = await ExecuteScoredSearch().execute(ctx)
        assert isinstance(result, StepResult)
        assert result.outcome == StepOutcome.SKIPPED
        assert rec.acquisitions == []
