"""RerankResults pipeline step tests.

Guards the three behaviours that matter: (1) it reorders raw_rows by the
ranker's scores, (2) the default noop keeps first-stage order exactly
(ship-dark), and (3) it degrades to first-stage order on the entity-lookup
skip path / empty pool.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search.rerank_results import RerankResults


def _row(content: str, similarity: float):
    return SimpleNamespace(
        Memory=SimpleNamespace(id=uuid.uuid4(), content=content, memory_type="fact"),
        similarity=similarity,
        vec_sim=similarity,
        freshness=1.0,
        score=similarity,
    )


@pytest.mark.asyncio
async def test_rerank_reorders_by_ranker_score():
    # First-stage order: unrelated first, "alpha" match last. The fake
    # ranker scores word overlap with the query, so it must promote the
    # "alpha" row to the front.
    r_unrelated = _row("totally unrelated", 0.9)
    r_match = _row("alpha alpha alpha", 0.1)
    ctx = PipelineContext(
        data={
            "raw_rows": [r_unrelated, r_match],
            "query": "alpha",
            "tenant_config": SimpleNamespace(rank_enabled=True, rank_provider="fake"),
        },
    )
    await RerankResults().execute(ctx)
    assert ctx.data["raw_rows"][0] is r_match
    assert ctx.data["raw_rows"][1] is r_unrelated


@pytest.mark.asyncio
async def test_disabled_by_default_skips():
    # RANK_ENABLED defaults false → the step is a zero-cost skip even with a
    # full pool present. This is the ship-dark / kill-switch guarantee.
    ctx = PipelineContext(data={"raw_rows": [_row("x", 0.5)], "query": "q"})
    result = await RerankResults().execute(ctx)
    assert result is not None and result.outcome.name == "SKIPPED"


@pytest.mark.asyncio
async def test_noop_keeps_first_stage_order():
    # Enabled + default provider (noop). Similarity is NOT descending in input
    # order; a correct noop must still keep the exact first-stage order.
    r0 = _row("x", 0.2)
    r1 = _row("y", 0.9)
    r2 = _row("z", 0.5)
    ctx = PipelineContext(
        data={
            "raw_rows": [r0, r1, r2],
            "query": "q",
            "tenant_config": SimpleNamespace(
                rank_enabled=True
            ),  # provider defaults noop
        },
    )
    await RerankResults().execute(ctx)
    assert ctx.data["raw_rows"] == [r0, r1, r2]


@pytest.mark.asyncio
async def test_skips_on_entity_lookup_plan():
    plan = SimpleNamespace(skip_scored_search=True)
    ctx = PipelineContext(
        data={
            "retrieval_plan": plan,
            "query": "q",
            "tenant_config": SimpleNamespace(rank_enabled=True),
        },
    )
    result = await RerankResults().execute(ctx)
    assert result is not None and result.outcome.name == "SKIPPED"


@pytest.mark.asyncio
async def test_no_raw_rows_is_skipped():
    # Enabled, but no candidate pool (e.g. upstream produced nothing) → skip.
    ctx = PipelineContext(
        data={"query": "q", "tenant_config": SimpleNamespace(rank_enabled=True)}
    )
    result = await RerankResults().execute(ctx)
    assert result is not None and result.outcome.name == "SKIPPED"
