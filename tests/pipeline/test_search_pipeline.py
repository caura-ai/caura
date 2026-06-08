"""Integration tests: search pipeline path vs legacy path produce equivalent output.

These tests require a running PostgreSQL instance (same as other integration tests).
They exercise both paths with identical inputs and compare the MemoryOut results.
"""

import uuid

import pytest

from core_api.schemas import MemoryCreate, MemoryOut

TENANT_ID = f"test-search-pipe-{uuid.uuid4().hex[:8]}"
FLEET_ID = "test-fleet"
AGENT_ID = "test-agent"


_SEED_CONTENTS = [
    "The quick brown fox jumped over the lazy dog on a sunny afternoon in the park near downtown.",
    "Alice prefers dark roast coffee every morning before her standup meeting at nine o'clock sharp.",
    "The quarterly budget review is scheduled for next Friday with the entire finance department attending.",
    "Bob mentioned he is allergic to peanuts and tree nuts, which is important for team lunch orders.",
    "The new deployment pipeline uses GitHub Actions with staging and production environments configured.",
]


async def _seed_memories(db, count: int = 3) -> list[MemoryOut]:
    """Insert test memories via the legacy write path and return them."""
    from core_api.services.memory_service import create_memory

    # Use a unique tenant per call to avoid cross-test dedup collisions
    tid = f"test-search-pipe-{uuid.uuid4().hex[:8]}"

    results = []
    for i in range(min(count, len(_SEED_CONTENTS))):
        data = MemoryCreate(
            tenant_id=tid,
            fleet_id=FLEET_ID,
            agent_id=AGENT_ID,
            content=_SEED_CONTENTS[i],
            persist=True,
            entity_links=[],
        )
        result = await create_memory(db, data)
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# Unit tests (no DB required)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_search_profile_defaults():
    """ResolveSearchProfile sets search_params with default constants."""
    from unittest.mock import AsyncMock

    from core_api.constants import MIN_SEARCH_SIMILARITY
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.resolve_search_profile import (
        ResolveSearchProfile,
    )

    ctx = PipelineContext(
        db=AsyncMock(),
        data={
            "query": "test query",
            "top_k": 5,
            "search_profile": None,
        },
    )
    step = ResolveSearchProfile()
    await step.execute(ctx)

    sp = ctx.data["search_params"]
    assert sp["top_k"] == 5
    assert sp["min_similarity"] == MIN_SEARCH_SIMILARITY
    assert "fts_weight" in sp
    assert "freshness_floor" in sp


@pytest.mark.asyncio
async def test_extract_temporal_hint_today():
    """ExtractTemporalHint detects 'today' as 1-day window."""
    from datetime import timedelta
    from unittest.mock import AsyncMock

    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.extract_temporal_hint import (
        ExtractTemporalHint,
    )

    ctx = PipelineContext(
        db=AsyncMock(),
        data={"query": "what happened today"},
    )
    step = ExtractTemporalHint()
    await step.execute(ctx)

    assert ctx.data["temporal_window"] == timedelta(days=1)


@pytest.mark.asyncio
async def test_extract_temporal_hint_none():
    """ExtractTemporalHint returns None for non-temporal queries."""
    from unittest.mock import AsyncMock

    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.extract_temporal_hint import (
        ExtractTemporalHint,
    )

    ctx = PipelineContext(
        db=AsyncMock(),
        data={"query": "favorite color"},
    )
    step = ExtractTemporalHint()
    await step.execute(ctx)

    assert ctx.data["temporal_window"] is None
    assert ctx.data["date_range_filter"] is None


@pytest.mark.asyncio
async def test_extract_temporal_hint_sets_date_range():
    """ExtractTemporalHint sets date_range_filter for temporal queries."""
    from unittest.mock import AsyncMock

    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.extract_temporal_hint import (
        ExtractTemporalHint,
    )

    ctx = PipelineContext(
        db=AsyncMock(),
        data={"query": "what happened two months ago"},
    )
    step = ExtractTemporalHint()
    await step.execute(ctx)

    assert ctx.data["date_range_filter"] is not None
    assert "start_date" in ctx.data["date_range_filter"]
    assert "end_date" in ctx.data["date_range_filter"]


@pytest.mark.asyncio
async def test_extract_temporal_hint_uses_valid_at_as_reference():
    """ExtractTemporalHint uses valid_at as reference datetime when available."""
    from datetime import datetime
    from unittest.mock import AsyncMock

    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.extract_temporal_hint import (
        ExtractTemporalHint,
    )

    ref = datetime(2026, 4, 14, 12, 0, 0)
    ctx = PipelineContext(
        db=AsyncMock(),
        data={"query": "notes from two weeks ago", "valid_at": ref},
    )
    step = ExtractTemporalHint()
    await step.execute(ctx)

    dr = ctx.data["date_range_filter"]
    assert dr is not None
    # 2 weeks = 14 days → target = 2026-03-31, range ±1 (week unit)
    assert dr["start_date"] == "2026-03-30"
    assert dr["end_date"] == "2026-04-01"


@pytest.mark.asyncio
async def test_post_filter_results():
    """PostFilterResults filters rows below min_similarity."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.post_filter_results import PostFilterResults

    rows = [
        SimpleNamespace(
            vec_sim=0.8, Memory=None, score=0.7, similarity=0.7, entity_links=[]
        ),
        SimpleNamespace(
            vec_sim=0.3, Memory=None, score=0.2, similarity=0.2, entity_links=[]
        ),
        SimpleNamespace(
            vec_sim=0.6, Memory=None, score=0.5, similarity=0.5, entity_links=[]
        ),
    ]
    ctx = PipelineContext(
        db=AsyncMock(),
        data={
            "raw_rows": rows,
            "search_params": {"min_similarity": 0.5},
        },
    )
    step = PostFilterResults()
    await step.execute(ctx)

    assert len(ctx.data["filtered_rows"]) == 2
    assert all(float(r.vec_sim) >= 0.5 for r in ctx.data["filtered_rows"])


@pytest.mark.asyncio
async def test_load_and_serialize_uses_preloaded_entity_links():
    """LoadAndSerialize reads entity_links from rows instead of querying DB."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from core_api.schemas import EntityLinkOut
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.load_and_serialize import LoadAndSerialize

    mem = MagicMock()
    mem.id = uuid.uuid4()
    mem.tenant_id = "t1"
    mem.fleet_id = "f1"
    mem.agent_id = "a1"
    mem.memory_type = "fact"
    mem.title = "Test"
    mem.content = "test content"
    mem.weight = 0.5
    mem.source_uri = None
    mem.run_id = None
    mem.metadata_ = None
    mem.created_at = datetime.now(timezone.utc)
    mem.expires_at = None
    mem.subject_entity_id = None
    mem.predicate = None
    mem.object_value = None
    mem.ts_valid_start = None
    mem.ts_valid_end = None
    mem.status = "active"
    mem.visibility = "scope_team"
    mem.recall_count = 0
    mem.last_recalled_at = None
    mem.supersedes_id = None

    entity_link = EntityLinkOut(entity_id=uuid.uuid4(), role="subject")
    row = SimpleNamespace(
        Memory=mem,
        score=0.85,
        similarity=0.8,
        vec_sim=0.9,
        entity_links=[entity_link],
    )

    mock_db = AsyncMock()
    ctx = PipelineContext(
        db=mock_db,
        data={"filtered_rows": [row]},
    )
    step = LoadAndSerialize()
    await step.execute(ctx)

    # DB should NOT have been called (entity links are pre-loaded)
    mock_db.execute.assert_not_called()

    results = ctx.data["results"]
    assert len(results) == 1
    assert len(results[0].entity_links) == 1
    assert results[0].entity_links[0].entity_id == entity_link.entity_id


@pytest.mark.asyncio
async def test_track_recalls_fire_and_forget():
    """TrackRecalls spawns a background task instead of awaiting on the request session."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock, patch

    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.track_recalls import TrackRecalls

    mem1 = MagicMock()
    mem1.id = uuid.uuid4()
    mem2 = MagicMock()
    mem2.id = uuid.uuid4()

    rows = [
        SimpleNamespace(Memory=mem1),
        SimpleNamespace(Memory=mem2),
    ]

    mock_db = AsyncMock()
    ctx = PipelineContext(
        db=mock_db,
        data={"filtered_rows": rows},
    )

    with patch("core_api.pipeline.steps.search.track_recalls.track_task") as mock_track:
        step = TrackRecalls()
        await step.execute(ctx)

        # track_task should have been called with a coroutine
        mock_track.assert_called_once()
        # Close the coroutine to avoid RuntimeWarning
        coro = mock_track.call_args[0][0]
        coro.close()

    # The request DB session should NOT have been used
    mock_db.execute.assert_not_called()
    mock_db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Fix A — Pipeline failure surfaces original error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_failure_includes_error_detail():
    """Pipeline failure HTTPException includes the original error message."""
    from unittest.mock import AsyncMock

    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.runner import Pipeline
    from core_api.pipeline.step import StepOutcome

    class FailingStep:
        @property
        def name(self):
            return "failing_step"

        async def execute(self, ctx):
            raise ValueError("something broke in scoring")

    ctx = PipelineContext(db=AsyncMock(), data={})
    pipeline = Pipeline("test", [FailingStep()])
    result = await pipeline.run(ctx)

    assert result.failed is True
    # Verify the error is stored in the step result
    failed = [s for s in result.steps if s.outcome == StepOutcome.FAILED]
    assert len(failed) == 1
    assert "something broke in scoring" in str(failed[0].error)


@pytest.mark.asyncio
async def test_search_pipeline_failure_logs_error_not_in_detail():
    """_search_memories_pipeline logs the error but does not leak it in the HTTP detail."""
    from unittest.mock import AsyncMock, patch

    from fastapi import HTTPException

    from core_api.pipeline.runner import Pipeline

    # Create a pipeline that fails
    class FailingStep:
        @property
        def name(self):
            return "failing_step"

        async def execute(self, ctx):
            raise ValueError("test error detail")

    with (
        patch(
            "core_api.pipeline.compositions.search.build_search_pipeline",
            return_value=Pipeline("search", [FailingStep()]),
        ),
        patch("core_api.services.memory_service.logger") as mock_logger,
    ):
        from core_api.services.memory_service import _search_memories_pipeline

        with pytest.raises(HTTPException) as exc_info:
            await _search_memories_pipeline(
                db=AsyncMock(),
                tenant_id="t1",
                query="test",
            )

        assert exc_info.value.status_code == 500
        # Error detail must NOT leak internal error messages
        assert "test error detail" not in exc_info.value.detail
        assert exc_info.value.detail == "Search pipeline failed unexpectedly"
        # But the error IS logged server-side
        mock_logger.error.assert_called_once()
        log_args = mock_logger.error.call_args
        assert "test error detail" in str(log_args)


# ---------------------------------------------------------------------------
# Fix D — Parallel embed timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_embed_gather_has_timeout():
    """ParallelEmbedAndEntityBoost has a timeout on asyncio.gather."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from fastapi import HTTPException

    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.parallel_embed_entity_boost import (
        ParallelEmbedAndEntityBoost,
    )

    ctx = PipelineContext(
        db=AsyncMock(),
        data={
            "query": "test",
            "tenant_id": "t1",
            "tenant_config": None,
            "search_params": {"graph_max_hops": 2},
            "graph_expand": True,
        },
    )

    async def slow_embed(*args, **kwargs):
        await asyncio.sleep(20)
        return [0.0] * 1536

    async def slow_boost(*args, **kwargs):
        await asyncio.sleep(20)
        return ([], {})

    with (
        patch(
            "core_api.pipeline.steps.search.parallel_embed_entity_boost._get_or_cache_embedding",
            side_effect=slow_embed,
        ),
        patch(
            "core_api.pipeline.steps.search.parallel_embed_entity_boost._entity_boost_via_storage",
            side_effect=slow_boost,
        ),
    ):
        step = ParallelEmbedAndEntityBoost()
        with pytest.raises(HTTPException) as exc_info:
            await asyncio.wait_for(step.execute(ctx), timeout=17.0)

        assert exc_info.value.status_code == 504


# ---------------------------------------------------------------------------
# Integration tests (require PostgreSQL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_search_returns_results(db):
    """Pipeline search path returns MemoryOut results for seeded memories."""
    from core_api.services import memory_service

    seeded = await _seed_memories(db, count=2)
    tid = seeded[0].tenant_id

    original = memory_service._USE_PIPELINE_SEARCH
    memory_service._USE_PIPELINE_SEARCH = True
    try:
        from core_api.services.memory_service import search_memories

        results = await search_memories(
            db,
            tenant_id=tid,
            query="quick brown fox",
            fleet_ids=[FLEET_ID],
            caller_agent_id=AGENT_ID,
        )
        assert isinstance(results, list)
        assert len(results) > 0
        assert all(isinstance(r, MemoryOut) for r in results)
    finally:
        memory_service._USE_PIPELINE_SEARCH = original


@pytest.mark.asyncio
async def test_legacy_search_returns_results(db):
    """Legacy search path returns results (baseline)."""
    from core_api.services import memory_service

    seeded = await _seed_memories(db, count=2)
    tid = seeded[0].tenant_id

    original = memory_service._USE_PIPELINE_SEARCH
    memory_service._USE_PIPELINE_SEARCH = False
    try:
        from core_api.services.memory_service import search_memories

        results = await search_memories(
            db,
            tenant_id=tid,
            query="quick brown fox",
            fleet_ids=[FLEET_ID],
            caller_agent_id=AGENT_ID,
        )
        assert isinstance(results, list)
        assert len(results) > 0
    finally:
        memory_service._USE_PIPELINE_SEARCH = original


@pytest.mark.asyncio
async def test_search_pipeline_equivalence(db):
    """Pipeline and legacy paths produce equivalent results (order, scores to 4 decimals, entity links)."""
    from core_api.services import memory_service
    from core_api.services.memory_service import (
        _search_memories_legacy,
        _search_memories_pipeline,
    )

    seeded = await _seed_memories(db, count=3)
    tid = seeded[0].tenant_id

    query = "quick brown fox sunny afternoon"
    # Disable recall_boost so the legacy path's recall tracking side-effect
    # (incrementing recall_count) doesn't change scores for the pipeline run.
    kwargs = {
        "tenant_id": tid,
        "query": query,
        "fleet_ids": [FLEET_ID],
        "caller_agent_id": AGENT_ID,
        "recall_boost": False,
    }

    memory_service._USE_PIPELINE_SEARCH = False
    legacy_results = await _search_memories_legacy(db, **kwargs)

    memory_service._USE_PIPELINE_SEARCH = True
    pipeline_results = await _search_memories_pipeline(db, **kwargs)
    memory_service._USE_PIPELINE_SEARCH = False

    assert len(legacy_results) == len(pipeline_results), (
        f"Result count mismatch: legacy={len(legacy_results)}, pipeline={len(pipeline_results)}"
    )

    for i, (leg, pip) in enumerate(zip(legacy_results, pipeline_results)):
        assert leg.id == pip.id, f"Row {i}: ID mismatch {leg.id} != {pip.id}"
        assert leg.similarity == pip.similarity, (
            f"Row {i}: score mismatch {leg.similarity} != {pip.similarity}"
        )
        leg_links = sorted([(el.entity_id, el.role) for el in leg.entity_links])
        pip_links = sorted([(el.entity_id, el.role) for el in pip.entity_links])
        assert leg_links == pip_links, f"Row {i}: entity_links mismatch"


@pytest.mark.asyncio
async def test_search_pipeline_empty_results(db):
    """Pipeline search returns empty list for no-match query."""
    from core_api.services import memory_service

    original = memory_service._USE_PIPELINE_SEARCH
    memory_service._USE_PIPELINE_SEARCH = True
    try:
        from core_api.services.memory_service import search_memories

        results = await search_memories(
            db,
            tenant_id=f"nonexistent-tenant-{uuid.uuid4().hex[:8]}",
            query="zzz no match zzz",
        )
        assert results == []
    finally:
        memory_service._USE_PIPELINE_SEARCH = original
