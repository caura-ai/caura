"""ANN-pool shadow mode (``ann_pool_shadow``) — serve legacy, compare pooled.

The contract: with ``ann_pool_shadow=1`` and ``ann_pool_size>0``,
``ExecuteScoredSearch`` serves the LEGACY full-scan result (the primary wire
payload carries ``ann_pool_size=0``), spawns one background task that re-runs
the same payload with the configured pool size, and logs a single grep-shaped
comparison line. Shadow is inert when the flag is off, when the pool size is
0, and on diagnostic runs.

All mocks — the storage client is an AsyncMock and the per-tenant slot is the
real in-process semaphore. The spawned task handle is stashed on
``ctx.data["_ann_shadow_task"]`` so tests can await it deterministically.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search.execute_scored_search import ExecuteScoredSearch

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_SP = {
    "fts_weight": 0.3,
    "graph_max_hops": 2,
    "top_k": 5,
    "min_similarity": 0.0,
    "freshness_floor": 0.7,
    "freshness_decay_days": 90,
    "recall_boost_cap": 1.1,
    "recall_decay_window_days": 14,
    "similarity_blend": 0.85,
    "fts_rank_scale": 6.0,
}


def _ctx(**sp_extra) -> PipelineContext:
    data: dict[str, Any] = {
        "query": "what changed in the rollout",
        "tenant_id": "t-shadow",
        "search_params": {**_SP, **sp_extra},
        "embedding": [0.0] * 8,
        "temporal_window": None,
        "boosted_memory_ids": set(),
        "memory_boost_factor": {},
    }
    return PipelineContext(data=data)


def _sc(rows_by_call: list[list[dict]]) -> AsyncMock:
    sc = AsyncMock()
    sc.scored_search = AsyncMock(side_effect=rows_by_call)
    return sc


_PRIMARY_ROWS = [
    {"id": "m1", "score": 0.9, "content": "a"},
    {"id": "m2", "score": 0.8, "content": "b"},
    {"id": "m3", "score": 0.7, "content": "c"},
]
_SHADOW_ROWS = [
    {"id": "m1", "score": 0.91, "content": "a"},
    {"id": "m4", "score": 0.5, "content": "d"},
]


@patch("core_api.pipeline.steps.search.execute_scored_search.get_storage_client")
async def test_shadow_serves_legacy_and_compares_pooled(mock_get_sc, caplog) -> None:
    sc = _sc([_PRIMARY_ROWS, _SHADOW_ROWS])
    mock_get_sc.return_value = sc
    ctx = _ctx(ann_pool_size=200, ann_pool_shadow=1)

    with caplog.at_level(logging.INFO):
        await ExecuteScoredSearch().execute(ctx)
        task = ctx.data.get("_ann_shadow_task")
        assert task is not None, "shadow task was not spawned"
        await task

    # Served result is the PRIMARY (legacy) rows.
    assert [r.Memory.id for r in ctx.data["raw_rows"]] == ["m1", "m2", "m3"]

    # Primary wire payload forced legacy; shadow restored the configured pool.
    assert sc.scored_search.await_count == 2
    primary_payload = sc.scored_search.await_args_list[0].args[0]
    shadow_payload = sc.scored_search.await_args_list[1].args[0]
    assert primary_payload["search_params"]["ann_pool_size"] == 0
    assert shadow_payload["search_params"]["ann_pool_size"] == 200
    # Everything else identical — same query, same knobs, same filters.
    assert {k: v for k, v in shadow_payload.items() if k != "search_params"} == {
        k: v for k, v in primary_payload.items() if k != "search_params"
    }
    assert {
        k: v for k, v in shadow_payload["search_params"].items() if k != "ann_pool_size"
    } == {
        k: v
        for k, v in primary_payload["search_params"].items()
        if k != "ann_pool_size"
    }

    line = next(m for m in caplog.messages if m.startswith("ann_pool_shadow:"))
    # k = final_top_k = 5; overlap m1 only; jaccard 1/4; top1 matches.
    assert "tenant=t-shadow" in line
    assert "k=5" in line
    assert "overlap=1" in line
    assert "jaccard=0.250" in line
    assert "top1_match=True" in line
    assert "primary_n=3" in line and "shadow_n=2" in line
    assert "shadow_underfilled=True" in line


@patch("core_api.pipeline.steps.search.execute_scored_search.get_storage_client")
async def test_no_shadow_when_flag_off(mock_get_sc) -> None:
    sc = _sc([_PRIMARY_ROWS])
    mock_get_sc.return_value = sc
    ctx = _ctx(ann_pool_size=200)  # pooled serves for real; nothing to compare

    await ExecuteScoredSearch().execute(ctx)

    assert sc.scored_search.await_count == 1
    assert "_ann_shadow_task" not in ctx.data
    # The pooled size crosses the wire untouched.
    assert sc.scored_search.await_args.args[0]["search_params"]["ann_pool_size"] == 200


@patch("core_api.pipeline.steps.search.execute_scored_search.get_storage_client")
async def test_no_shadow_when_pool_size_zero(mock_get_sc) -> None:
    sc = _sc([_PRIMARY_ROWS])
    mock_get_sc.return_value = sc
    ctx = _ctx(ann_pool_shadow=1)  # flag without a pool is inert

    await ExecuteScoredSearch().execute(ctx)

    assert sc.scored_search.await_count == 1
    assert "_ann_shadow_task" not in ctx.data


@patch("core_api.pipeline.steps.search.execute_scored_search.get_storage_client")
async def test_no_shadow_on_diagnostic_runs(mock_get_sc) -> None:
    sc = _sc([_PRIMARY_ROWS])
    mock_get_sc.return_value = sc
    ctx = _ctx(ann_pool_size=200, ann_pool_shadow=1)
    ctx.data["diagnostic"] = True

    await ExecuteScoredSearch().execute(ctx)

    assert sc.scored_search.await_count == 1
    assert "_ann_shadow_task" not in ctx.data
    # Diagnostic runs serve the configured mode directly — no forced-legacy.
    assert sc.scored_search.await_args.args[0]["search_params"]["ann_pool_size"] == 200


@patch("core_api.pipeline.steps.search.execute_scored_search.get_storage_client")
async def test_shadow_failure_never_reaches_the_caller(mock_get_sc, caplog) -> None:
    """tracked_task absorbs shadow errors; the served result is unaffected."""
    sc = AsyncMock()
    sc.scored_search = AsyncMock(
        side_effect=[_PRIMARY_ROWS, RuntimeError("storage down")]
    )
    mock_get_sc.return_value = sc
    ctx = _ctx(ann_pool_size=200, ann_pool_shadow=1)

    with caplog.at_level(logging.INFO):
        await ExecuteScoredSearch().execute(ctx)
        await ctx.data["_ann_shadow_task"]  # must not raise

    assert [r.Memory.id for r in ctx.data["raw_rows"]] == ["m1", "m2", "m3"]
    assert not any(m.startswith("ann_pool_shadow:") for m in caplog.messages)


# ── D12 arm provenance surfaces in the diagnostic trace ─────────────────────


async def test_diagnostic_trace_carries_pool_arms() -> None:
    from types import SimpleNamespace

    from core_api.pipeline.steps.search.post_filter_results import PostFilterResults

    row = SimpleNamespace(
        Memory=SimpleNamespace(
            id="m1", title=None, memory_type="fact", status="active"
        ),
        score=0.9,
        vec_sim=0.9,
        fts_score=None,
        fts_match=False,
        freshness=1.0,
        entity_boost=1.0,
        recall_boost=1.0,
        temporal_boost=1.0,
        status_penalty=1.0,
        has_embedding=True,
        pool_arms="ann+fts",
    )
    ctx = PipelineContext(
        data={
            "search_params": {"min_similarity": 0.0, "fts_weight": 0.3},
            "raw_rows": [row],
            "diagnostic": True,
            "final_top_k": 5,
        }
    )
    await PostFilterResults().execute(ctx)
    assert ctx.data["diagnostic_results"][0]["pool_arms"] == "ann+fts"


# ── validation: the pool selectors are mutually exclusive ───────────────────


async def test_default_profile_rejects_both_pool_selectors() -> None:
    from core_api.services.organization_settings import _validate_default_search_profile

    with pytest.raises(ValueError, match="mutually exclusive"):
        _validate_default_search_profile(
            {
                "search": {
                    "default_profile": {"ann_pool_size": 100, "candidate_pool_size": 50}
                }
            }
        )


async def test_default_profile_accepts_each_selector_alone() -> None:
    from core_api.services.organization_settings import _validate_default_search_profile

    _validate_default_search_profile(
        {"search": {"default_profile": {"ann_pool_size": 100}}}
    )
    _validate_default_search_profile(
        {"search": {"default_profile": {"candidate_pool_size": 50}}}
    )
    _validate_default_search_profile(
        {"search": {"default_profile": {"ann_pool_size": 100, "ann_pool_shadow": 1}}}
    )


async def test_shadow_knob_is_clamped_by_the_table() -> None:
    from core_api.services.organization_settings import validate_search_profile

    assert validate_search_profile({"ann_pool_shadow": 5})["ann_pool_shadow"] == 1
    assert validate_search_profile({"ann_pool_shadow": 1})["ann_pool_shadow"] == 1


@patch("core_api.pipeline.steps.search.execute_scored_search.get_storage_client")
async def test_score_delta_is_scoped_to_the_topk_window(mock_get_sc, caplog) -> None:
    """Every metric in the log line describes the SAME top-k window.

    The shadow fixture carries a shared id BEYOND k with a wild score delta
    (m2: 0.8 primary vs 0.2 shadow). Unscoped, it would drag
    mean_abs_score_delta to ~0.305; scoped to the k-window intersection the
    only shared id is m1 and the mean is exactly 0.0100.
    """
    primary = [
        {"id": "m1", "score": 0.9, "content": "a"},
        {"id": "m2", "score": 0.8, "content": "b"},
        {"id": "m3", "score": 0.7, "content": "c"},
    ]
    shadow = [
        {"id": "m1", "score": 0.91, "content": "a"},
        {"id": "m5", "score": 0.85, "content": "e"},
        {
            "id": "m2",
            "score": 0.2,
            "content": "b",
        },  # shared, but beyond k on both sides
    ]
    sc = _sc([primary, shadow])
    mock_get_sc.return_value = sc
    ctx = _ctx(ann_pool_size=200, ann_pool_shadow=1, top_k=2)

    with caplog.at_level(logging.INFO):
        await ExecuteScoredSearch().execute(ctx)
        await ctx.data["_ann_shadow_task"]

    line = next(m for m in caplog.messages if m.startswith("ann_pool_shadow:"))
    assert "k=2" in line
    assert "mean_abs_score_delta=0.0100" in line, line
