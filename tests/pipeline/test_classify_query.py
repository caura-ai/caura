"""Unit tests for ClassifyQuery pipeline step (query dispatcher).

All tests use mocks — no real DB required.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from core_api.constants import FTS_WEIGHT_BOOSTED
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult
from core_api.pipeline.steps.search.classify_query import ClassifyQuery
from core_api.pipeline.steps.search.retrieval_types import (
    RetrievalPlan,
    RetrievalStrategy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_SEARCH_PARAMS = {
    "fts_weight": 0.3,
    "graph_max_hops": 2,
    "top_k": 10,
    "min_similarity": 0.0,
}


def _make_ctx(query: str, *, fts_weight: float = 0.3, **extra_data) -> PipelineContext:
    sp = {**_DEFAULT_SEARCH_PARAMS, "fts_weight": fts_weight}
    data = {
        "query": query,
        "tenant_id": "t1",
        "fleet_ids": ["fleet-1"],
        "search_params": sp,
        **extra_data,
    }
    return PipelineContext(db=AsyncMock(), data=data)


def _mock_sc(**overrides) -> AsyncMock:
    """Create a mock storage client with sensible defaults."""
    sc = AsyncMock()
    sc.fts_search_entities = AsyncMock(return_value=[])
    sc.expand_graph = AsyncMock(return_value={})
    sc.get_memory_ids_by_entity_ids = AsyncMock(return_value=[])
    # CAURA-687: ENTITY_LOOKUP short-circuit now calls load_memories_by_ids
    # (dedicated endpoint) instead of scored_search. scored_search default
    # kept for legacy test paths that may still reference it.
    sc.load_memories_by_ids = AsyncMock(return_value=[])
    sc.scored_search = AsyncMock(return_value=[])
    for k, v in overrides.items():
        setattr(sc, k, v)
    return sc


# ---------------------------------------------------------------------------
# Classification tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("core_api.pipeline.steps.search.classify_query.get_storage_client")
async def test_semantic_for_long_query(mock_get_sc):
    """Long natural-language query with low fts_weight routes to SEMANTIC_SEARCH."""
    mock_get_sc.return_value = _mock_sc()
    ctx = _make_ctx("what do we know about our pricing strategy for next quarter")

    step = ClassifyQuery()
    await step.execute(ctx)

    plan: RetrievalPlan = ctx.data["retrieval_plan"]
    assert plan.strategy == RetrievalStrategy.SEMANTIC_SEARCH
    assert plan.skip_embedding is False
    assert plan.skip_scored_search is False


@pytest.mark.asyncio
@patch("core_api.pipeline.steps.search.classify_query.get_storage_client")
async def test_keyword_for_specific_query(mock_get_sc):
    """Short specific query with FTS_WEIGHT_BOOSTED routes to KEYWORD_SEARCH."""
    mock_get_sc.return_value = _mock_sc()
    ctx = _make_ctx("OpenAI", fts_weight=FTS_WEIGHT_BOOSTED)

    step = ClassifyQuery()
    await step.execute(ctx)

    plan: RetrievalPlan = ctx.data["retrieval_plan"]
    assert plan.strategy == RetrievalStrategy.KEYWORD_SEARCH
    assert plan.skip_embedding is False
    assert plan.skip_scored_search is False


@pytest.mark.asyncio
@patch("core_api.pipeline.steps.search.classify_query.get_storage_client")
async def test_entity_lookup_with_match(mock_get_sc):
    """Entity match triggers ENTITY_LOOKUP with skip flags and populated filtered_rows."""
    ctx = _make_ctx("Alice")

    entity_id = uuid.uuid4()
    memory_id = uuid.uuid4()
    eid_str = str(entity_id)
    mid_str = str(memory_id)

    sc = _mock_sc()
    sc.fts_search_entities = AsyncMock(return_value=[eid_str])
    sc.expand_graph = AsyncMock(return_value={eid_str: {"hop": 0, "weight": 1.0}})
    sc.get_memory_ids_by_entity_ids = AsyncMock(
        return_value=[{"memory_id": mid_str, "entity_id": eid_str, "role": "subject"}]
    )
    # CAURA-687: ENTITY_LOOKUP path now calls load_memories_by_ids.
    sc.load_memories_by_ids = AsyncMock(
        return_value=[
            {
                "id": mid_str,
                "tenant_id": "t1",
                "content": "Alice test memory",
                "memory_type": "fact",
            }
        ]
    )
    mock_get_sc.return_value = sc

    step = ClassifyQuery()
    await step.execute(ctx)

    plan: RetrievalPlan = ctx.data["retrieval_plan"]
    assert plan.strategy == RetrievalStrategy.ENTITY_LOOKUP
    assert plan.skip_embedding is True
    assert plan.skip_scored_search is True
    assert len(ctx.data["filtered_rows"]) > 0


@pytest.mark.asyncio
@patch("core_api.pipeline.steps.search.classify_query.get_storage_client")
async def test_entity_lookup_no_linked_memories(mock_get_sc):
    """Entity match found but no linked memories falls through to keyword/semantic."""
    ctx = _make_ctx("Alice")

    entity_id = uuid.uuid4()
    eid_str = str(entity_id)

    sc = _mock_sc()
    sc.fts_search_entities = AsyncMock(return_value=[eid_str])
    sc.expand_graph = AsyncMock(return_value={eid_str: {"hop": 0, "weight": 1.0}})
    sc.get_memory_ids_by_entity_ids = AsyncMock(return_value=[])
    mock_get_sc.return_value = sc

    step = ClassifyQuery()
    await step.execute(ctx)

    plan: RetrievalPlan = ctx.data["retrieval_plan"]
    assert plan.strategy == RetrievalStrategy.SEMANTIC_SEARCH
    assert "filtered_rows" not in ctx.data
    assert "_classified_entity_hops" in ctx.data
    assert ctx.data["_classified_entity_hops"] == {entity_id: (0, 1.0)}


@pytest.mark.asyncio
@patch("core_api.pipeline.steps.search.classify_query.get_storage_client")
async def test_no_entity_match_falls_through(mock_get_sc):
    """Query with entity-like token but no FTS match falls through to SEMANTIC_SEARCH."""
    mock_get_sc.return_value = _mock_sc()
    ctx = _make_ctx("Alice", fts_weight=0.3)

    step = ClassifyQuery()
    await step.execute(ctx)

    plan: RetrievalPlan = ctx.data["retrieval_plan"]
    assert plan.strategy == RetrievalStrategy.SEMANTIC_SEARCH


@pytest.mark.asyncio
async def test_empty_query():
    """Empty query with no tokens routes to SEMANTIC_SEARCH."""
    ctx = _make_ctx("")

    step = ClassifyQuery()
    await step.execute(ctx)

    plan: RetrievalPlan = ctx.data["retrieval_plan"]
    assert plan.strategy == RetrievalStrategy.SEMANTIC_SEARCH


@pytest.mark.asyncio
@patch("core_api.pipeline.steps.search.classify_query.get_storage_client")
async def test_uuid_laden_gibberish_drops_hex_fragments(mock_get_sc):
    """Hex chunks inside hyphenated tokens must not reach entity FTS."""
    sc = _mock_sc()
    mock_get_sc.return_value = sc

    hex_chunk = "deadbeefcafe1234deadbeefcafe1234"
    ctx = _make_ctx(f"xyzzyzombie-{hex_chunk}-FLIBBERTYJIBBET")

    step = ClassifyQuery()
    await step.execute(ctx)

    sent_tokens = sc.fts_search_entities.await_args.args[0]["tokens"]
    assert hex_chunk not in sent_tokens
    assert "xyzzyzombie" in sent_tokens
    assert "flibbertyjibbet" in sent_tokens


@pytest.mark.asyncio
@patch("core_api.pipeline.steps.search.classify_query.get_storage_client")
async def test_internal_punctuation_splits_tokens(mock_get_sc):
    """Hyphenated phrases split client-side, not via storage FTS re-tokenisation."""
    sc = _mock_sc()
    mock_get_sc.return_value = sc

    ctx = _make_ctx("USA-based,research/scribe")

    step = ClassifyQuery()
    await step.execute(ctx)

    sent_tokens = sc.fts_search_entities.await_args.args[0]["tokens"]
    assert "usa" in sent_tokens
    assert "based" in sent_tokens
    assert "research" in sent_tokens
    assert "scribe" in sent_tokens


@pytest.mark.asyncio
@patch("core_api.pipeline.steps.search.classify_query.get_storage_client")
async def test_uuid_query_long_hex_segments_dropped(mock_get_sc):
    """A pasted UUID — segments ≥8 chars are dropped (UUID-shaped), shorter
    segments survive (collide with real English words like ``cafe``).
    With no entity match the query falls through to SEMANTIC_SEARCH."""
    sc = _mock_sc()
    mock_get_sc.return_value = sc

    ctx = _make_ctx("550e8400-e29b-41d4-a716-446655440000")

    step = ClassifyQuery()
    await step.execute(ctx)

    sent_tokens = sc.fts_search_entities.await_args.args[0]["tokens"]
    assert "550e8400" not in sent_tokens
    assert "446655440000" not in sent_tokens
    assert sent_tokens == ["e29b", "41d4", "a716"]
    plan: RetrievalPlan = ctx.data["retrieval_plan"]
    assert plan.strategy == RetrievalStrategy.SEMANTIC_SEARCH


# ---------------------------------------------------------------------------
# TEMPORAL routing tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("core_api.pipeline.steps.search.classify_query.get_storage_client")
async def test_temporal_routes_when_temporal_window_set(mock_get_sc):
    """Query with temporal_window set routes to TEMPORAL strategy."""
    mock_get_sc.return_value = _mock_sc()
    ctx = _make_ctx("what happened this week", temporal_window=timedelta(days=7))

    step = ClassifyQuery()
    await step.execute(ctx)

    plan: RetrievalPlan = ctx.data["retrieval_plan"]
    assert plan.strategy == RetrievalStrategy.TEMPORAL
    assert plan.skip_embedding is False
    assert plan.skip_scored_search is False
    assert plan.search_param_overrides["freshness_decay_days"] == 7
    assert plan.search_param_overrides["freshness_floor"] == 0.3


@pytest.mark.asyncio
@patch("core_api.pipeline.steps.search.classify_query.get_storage_client")
async def test_temporal_does_not_route_without_window(mock_get_sc):
    """No temporal_window → falls through to keyword/semantic."""
    mock_get_sc.return_value = _mock_sc()
    ctx = _make_ctx("tell me about kafka")

    step = ClassifyQuery()
    await step.execute(ctx)

    plan: RetrievalPlan = ctx.data["retrieval_plan"]
    assert plan.strategy == RetrievalStrategy.SEMANTIC_SEARCH


@pytest.mark.asyncio
@patch("core_api.pipeline.steps.search.classify_query.get_storage_client")
async def test_entity_lookup_takes_priority_over_temporal(mock_get_sc):
    """Entity match + temporal_window set → ENTITY_LOOKUP wins."""
    entity_id = uuid.uuid4()
    memory_id = uuid.uuid4()
    eid_str = str(entity_id)
    mid_str = str(memory_id)

    sc = _mock_sc()
    sc.fts_search_entities = AsyncMock(return_value=[eid_str])
    sc.expand_graph = AsyncMock(return_value={eid_str: {"hop": 0, "weight": 1.0}})
    sc.get_memory_ids_by_entity_ids = AsyncMock(
        return_value=[{"memory_id": mid_str, "entity_id": eid_str, "role": "subject"}]
    )
    sc.load_memories_by_ids = AsyncMock(
        return_value=[
            {
                "id": mid_str,
                "tenant_id": "t1",
                "content": "Alice temporal test",
                "memory_type": "fact",
            }
        ]
    )
    mock_get_sc.return_value = sc

    ctx = _make_ctx("Alice", temporal_window=timedelta(days=7))

    step = ClassifyQuery()
    await step.execute(ctx)

    plan: RetrievalPlan = ctx.data["retrieval_plan"]
    assert plan.strategy == RetrievalStrategy.ENTITY_LOOKUP


@pytest.mark.asyncio
@patch("core_api.pipeline.steps.search.classify_query.get_storage_client")
async def test_temporal_takes_priority_over_recent(mock_get_sc):
    """temporal_window set + recency keywords → TEMPORAL wins."""
    mock_get_sc.return_value = _mock_sc()
    ctx = _make_ctx(
        "what was I working on this week", temporal_window=timedelta(days=7)
    )

    step = ClassifyQuery()
    await step.execute(ctx)

    plan: RetrievalPlan = ctx.data["retrieval_plan"]
    assert plan.strategy == RetrievalStrategy.TEMPORAL


# ---------------------------------------------------------------------------
# RECENT_CONTEXT routing tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("core_api.pipeline.steps.search.classify_query.get_storage_client")
async def test_recent_context_routes_on_keyword(mock_get_sc):
    """Recency keyword routes to RECENT_CONTEXT strategy."""
    mock_get_sc.return_value = _mock_sc()
    ctx = _make_ctx("what was I working on")

    step = ClassifyQuery()
    await step.execute(ctx)

    plan: RetrievalPlan = ctx.data["retrieval_plan"]
    assert plan.strategy == RetrievalStrategy.RECENT_CONTEXT
    assert plan.search_param_overrides["freshness_decay_days"] == 7
    assert plan.search_param_overrides["freshness_floor"] == 0.2
    assert plan.search_param_overrides["top_k"] == 5


@pytest.mark.asyncio
@patch("core_api.pipeline.steps.search.classify_query.get_storage_client")
async def test_recent_context_respects_agent_top_k(mock_get_sc):
    """RECENT_CONTEXT caps top_k at min(agent_top_k, 5)."""
    mock_get_sc.return_value = _mock_sc()
    ctx = _make_ctx("my latest updates")
    ctx.data["search_params"]["top_k"] = 3

    step = ClassifyQuery()
    await step.execute(ctx)

    plan: RetrievalPlan = ctx.data["retrieval_plan"]
    assert plan.strategy == RetrievalStrategy.RECENT_CONTEXT
    assert plan.search_param_overrides["top_k"] == 3


@pytest.mark.asyncio
@patch("core_api.pipeline.steps.search.classify_query.get_storage_client")
async def test_recent_context_no_match_falls_through(mock_get_sc):
    """Query without recency keywords falls through to SEMANTIC_SEARCH."""
    mock_get_sc.return_value = _mock_sc()
    ctx = _make_ctx("tell me about kafka")

    step = ClassifyQuery()
    await step.execute(ctx)

    plan: RetrievalPlan = ctx.data["retrieval_plan"]
    assert plan.strategy == RetrievalStrategy.SEMANTIC_SEARCH


@pytest.mark.asyncio
@patch("core_api.pipeline.steps.search.classify_query.get_storage_client")
async def test_entity_lookup_takes_priority_over_recent(mock_get_sc):
    """Entity match + recency keywords → ENTITY_LOOKUP wins."""
    entity_id = uuid.uuid4()
    memory_id = uuid.uuid4()
    eid_str = str(entity_id)
    mid_str = str(memory_id)

    sc = _mock_sc()
    sc.fts_search_entities = AsyncMock(return_value=[eid_str])
    sc.expand_graph = AsyncMock(return_value={eid_str: {"hop": 0, "weight": 1.0}})
    sc.get_memory_ids_by_entity_ids = AsyncMock(
        return_value=[{"memory_id": mid_str, "entity_id": eid_str, "role": "subject"}]
    )
    sc.load_memories_by_ids = AsyncMock(
        return_value=[
            {
                "id": mid_str,
                "tenant_id": "t1",
                "content": "Alice recent test",
                "memory_type": "fact",
            }
        ]
    )
    mock_get_sc.return_value = sc

    ctx = _make_ctx("what was I doing with Alice")

    step = ClassifyQuery()
    await step.execute(ctx)

    plan: RetrievalPlan = ctx.data["retrieval_plan"]
    assert plan.strategy == RetrievalStrategy.ENTITY_LOOKUP


@pytest.mark.asyncio
@patch("core_api.pipeline.steps.search.classify_query.get_storage_client")
async def test_search_param_overrides_in_plan(mock_get_sc):
    """TEMPORAL plan has non-empty search_param_overrides dict."""
    mock_get_sc.return_value = _mock_sc()
    ctx = _make_ctx("what happened yesterday", temporal_window=timedelta(days=2))

    step = ClassifyQuery()
    await step.execute(ctx)

    plan: RetrievalPlan = ctx.data["retrieval_plan"]
    assert plan.strategy == RetrievalStrategy.TEMPORAL
    assert len(plan.search_param_overrides) > 0
    assert "freshness_decay_days" in plan.search_param_overrides


# ---------------------------------------------------------------------------
# Parallel fleet expansion tests (Fix F)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expand_per_fleet_parallel_merges_correctly():
    """Parallel fleet expansion merges by lowest hop per entity."""
    eid_a = uuid.uuid4()
    eid_b = uuid.uuid4()
    eid_c = uuid.uuid4()

    sc = AsyncMock()
    sc.expand_graph = AsyncMock(
        side_effect=[
            {str(eid_a): {"hop": 0, "weight": 1.0}, str(eid_b): {"hop": 2, "weight": 0.5}},
            {
                str(eid_a): {"hop": 1, "weight": 0.8},
                str(eid_b): {"hop": 1, "weight": 0.9},
                str(eid_c): {"hop": 0, "weight": 1.0},
            },
        ]
    )

    result = await ClassifyQuery._expand_per_fleet(
        sc=sc,
        seed_ids=[eid_a],
        tenant_id="t1",
        fleet_ids=["f1", "f2"],
        max_hops=2,
    )

    assert result[eid_a] == (0, 1.0)  # fleet 1 wins (hop 0 < hop 1)
    assert result[eid_b] == (1, 0.9)  # fleet 2 wins (hop 1 < hop 2)
    assert result[eid_c] == (0, 1.0)  # only in fleet 2
    assert sc.expand_graph.call_count == 2


@pytest.mark.asyncio
async def test_expand_per_fleet_partial_failure():
    """A single fleet failure is logged and skipped; other fleets still contribute."""
    eid_a = uuid.uuid4()

    sc = AsyncMock()
    sc.expand_graph = AsyncMock(
        side_effect=[
            {str(eid_a): {"hop": 0, "weight": 1.0}},  # fleet 1 succeeds
            RuntimeError("DB connection lost"),  # fleet 2 fails
        ]
    )

    result = await ClassifyQuery._expand_per_fleet(
        sc=sc,
        seed_ids=[eid_a],
        tenant_id="t1",
        fleet_ids=["f1", "f2"],
        max_hops=2,
    )

    assert result[eid_a] == (0, 1.0)
    assert sc.expand_graph.call_count == 2


# ---------------------------------------------------------------------------
# Entity cap tests (Fix G)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_memories_caps_entity_ids():
    """_collect_memories caps entity IDs before the IN query."""
    # Create 300 entity IDs (exceeds GRAPH_MAX_EXPANDED_ENTITIES=200)
    entity_hops = {uuid.uuid4(): (i % 3, 1.0 - i * 0.001) for i in range(300)}

    sc = AsyncMock()
    sc.get_memory_ids_by_entity_ids = AsyncMock(return_value=[])
    sc.load_memories_by_ids = AsyncMock(return_value=[])

    result = await ClassifyQuery._collect_memories(
        sc=sc,
        entity_hops=entity_hops,
        tenant_id="t1",
        top_k=10,
    )

    sc.get_memory_ids_by_entity_ids.assert_called_once()
    assert result == []


# ---------------------------------------------------------------------------
# Skip guard tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_step_skips_on_entity_lookup():
    """ParallelEmbedAndEntityBoost skips when retrieval_plan has skip_embedding=True."""
    from core_api.pipeline.steps.search import ParallelEmbedAndEntityBoost

    ctx = _make_ctx("Alice")
    ctx.data["retrieval_plan"] = RetrievalPlan(
        strategy=RetrievalStrategy.ENTITY_LOOKUP,
        skip_embedding=True,
    )

    step = ParallelEmbedAndEntityBoost()
    result = await step.execute(ctx)

    assert isinstance(result, StepResult)
    assert result.outcome == StepOutcome.SKIPPED


@pytest.mark.asyncio
async def test_scored_search_skips_on_entity_lookup():
    """ExecuteScoredSearch skips when retrieval_plan has skip_scored_search=True."""
    from core_api.pipeline.steps.search import ExecuteScoredSearch

    ctx = _make_ctx("Alice")
    ctx.data["retrieval_plan"] = RetrievalPlan(
        strategy=RetrievalStrategy.ENTITY_LOOKUP,
        skip_scored_search=True,
    )

    step = ExecuteScoredSearch()
    result = await step.execute(ctx)

    assert isinstance(result, StepResult)
    assert result.outcome == StepOutcome.SKIPPED


@pytest.mark.asyncio
async def test_post_filter_skips_on_entity_lookup():
    """PostFilterResults skips when retrieval_plan strategy is ENTITY_LOOKUP."""
    from core_api.pipeline.steps.search import PostFilterResults

    ctx = _make_ctx("Alice")
    ctx.data["retrieval_plan"] = RetrievalPlan(
        strategy=RetrievalStrategy.ENTITY_LOOKUP,
        skip_embedding=True,
        skip_scored_search=True,
    )
    ctx.data["filtered_rows"] = []

    step = PostFilterResults()
    result = await step.execute(ctx)

    assert isinstance(result, StepResult)
    assert result.outcome == StepOutcome.SKIPPED


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backward_compat_no_plan():
    """PostFilterResults runs normally when no retrieval_plan is set."""
    from core_api.pipeline.steps.search import PostFilterResults

    ctx = _make_ctx("test query")
    ctx.data["raw_rows"] = []
    ctx.data["search_params"] = {"min_similarity": 0.0}

    step = PostFilterResults()
    result = await step.execute(ctx)

    assert result is None
    assert "filtered_rows" in ctx.data
