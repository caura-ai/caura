"""D7 — Per-task timeout for ParallelEmbedAndEntityBoost.

The step runs two coroutines concurrently under one wall-clock budget:

1. ``_get_or_cache_embedding`` — critical path. Without an embedding the
   search cannot run. A failure here MUST escalate to an ``HTTPException``
   (504 on timeout, 503 on ``ValueError``).
2. ``_entity_boost_via_storage`` — supplementary ranking signal. A failure
   (timeout OR any other exception) MUST degrade gracefully to an empty
   set/dict and emit a single WARNING log line; no exception escapes.

D7 (will-be PR #260) splits the previously-shared ``asyncio.gather`` budget
into a critical-path timeout for the embedding and a separate, smaller
remaining-budget timeout for the entity boost. The headline regression
the fix addresses: a slow entity task used to cancel a fast embedding
because both shared the same ``wait_for`` scope, surfacing as a spurious
504 even though the embedding had completed in <10 ms.

These tests pin every branch of the new contract. They are pure unit tests
(no DB) and finish in well under a second each by monkeypatching the two
module-level budget constants down to small values.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search import parallel_embed_entity_boost as mod
from core_api.pipeline.steps.search.parallel_embed_entity_boost import (
    ParallelEmbedAndEntityBoost,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMBED_PATH = (
    "core_api.pipeline.steps.search.parallel_embed_entity_boost"
    "._get_or_cache_embedding"
)
_BOOST_PATH = (
    "core_api.pipeline.steps.search.parallel_embed_entity_boost"
    "._entity_boost_via_storage"
)

_LOGGER_NAME = "core_api.pipeline.steps.search.parallel_embed_entity_boost"


def _make_ctx() -> PipelineContext:
    """Minimal PipelineContext shaped like ParallelEmbedAndEntityBoost expects.

    Mirrors the construction used in ``tests/pipeline/test_search_pipeline.py``
    (the D step's existing test) — same keys, same default ``graph_max_hops``.
    """
    return PipelineContext(
                data={
            "query": "test",
            "tenant_id": "t1",
            "tenant_config": None,
            "search_params": {"graph_max_hops": 2},
            "graph_expand": True,
            "fleet_ids": ["fleet-1"],
        },
    )


def _shrink_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the two budget knobs down so timeout-cases finish quickly.

    Without this every 504-path test would burn 15 s of wall-clock.
    """
    monkeypatch.setattr(mod, "_OVERALL_TIMEOUT_S", 0.3)
    monkeypatch.setattr(mod, "_MIN_ENTITY_BUDGET_S", 0.05)


_DUMMY_EMBED = [0.1] * 1536


# ---------------------------------------------------------------------------
# Case 1 — happy path
# ---------------------------------------------------------------------------


async def test_happy_path_writes_embedding_and_boost(monkeypatch):
    """Both helpers return quickly → all three ctx.data fields populated."""
    _shrink_budgets(monkeypatch)

    boosted = {"00000000-0000-0000-0000-000000000001"}
    factors = {"00000000-0000-0000-0000-000000000001": 1.5}

    async def fast_embed(*args, **kwargs):
        return _DUMMY_EMBED

    async def fast_boost(*args, **kwargs):
        return (boosted, factors)

    ctx = _make_ctx()
    with (
        patch(_EMBED_PATH, side_effect=fast_embed),
        patch(_BOOST_PATH, side_effect=fast_boost),
    ):
        step = ParallelEmbedAndEntityBoost()
        await step.execute(ctx)

    assert ctx.data["embedding"] == _DUMMY_EMBED
    assert ctx.data["boosted_memory_ids"] == boosted
    assert ctx.data["memory_boost_factor"] == factors


# ---------------------------------------------------------------------------
# Case 2 — embedding times out → HTTPException(504)
# ---------------------------------------------------------------------------


async def test_embedding_timeout_raises_504(monkeypatch):
    """Critical-path timeout escalates to 504; no partial writes."""
    _shrink_budgets(monkeypatch)

    async def slow_embed(*args, **kwargs):
        # Exceeds _OVERALL_TIMEOUT_S (0.3 s) — must trip the 504 path.
        await asyncio.sleep(5.0)
        return _DUMMY_EMBED

    async def fast_boost(*args, **kwargs):
        return (set(), {})

    ctx = _make_ctx()
    with (
        patch(_EMBED_PATH, side_effect=slow_embed),
        patch(_BOOST_PATH, side_effect=fast_boost),
    ):
        step = ParallelEmbedAndEntityBoost()
        with pytest.raises(HTTPException) as exc_info:
            await step.execute(ctx)

    assert exc_info.value.status_code == 504
    # No partial state written.
    assert "embedding" not in ctx.data
    assert "boosted_memory_ids" not in ctx.data
    assert "memory_boost_factor" not in ctx.data


# ---------------------------------------------------------------------------
# Case 3 — embedding raises ValueError → HTTPException(503)
# ---------------------------------------------------------------------------


async def test_embedding_value_error_raises_503(monkeypatch):
    """ValueError from embed → 503 with original message preserved."""
    _shrink_budgets(monkeypatch)

    async def bad_embed(*args, **kwargs):
        raise ValueError("embedding provider down: bork-bork-bork")

    async def fast_boost(*args, **kwargs):
        return (set(), {})

    ctx = _make_ctx()
    with (
        patch(_EMBED_PATH, side_effect=bad_embed),
        patch(_BOOST_PATH, side_effect=fast_boost),
    ):
        step = ParallelEmbedAndEntityBoost()
        with pytest.raises(HTTPException) as exc_info:
            await step.execute(ctx)

    assert exc_info.value.status_code == 503
    assert "bork-bork-bork" in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# Case 4 — entity boost times out, embedding succeeds → graceful fallback
# ---------------------------------------------------------------------------


async def test_entity_boost_timeout_falls_back_quietly(monkeypatch, caplog):
    """Slow entity task → empty boost + warning log; no exception."""
    _shrink_budgets(monkeypatch)
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    async def fast_embed(*args, **kwargs):
        await asyncio.sleep(0.01)
        return _DUMMY_EMBED

    async def slow_boost(*args, **kwargs):
        # Exceeds the patched _MIN_ENTITY_BUDGET_S (0.05 s) but well
        # under _OVERALL_TIMEOUT_S so the embedding completes first.
        await asyncio.sleep(5.0)
        return (set(), {})

    ctx = _make_ctx()
    with (
        patch(_EMBED_PATH, side_effect=fast_embed),
        patch(_BOOST_PATH, side_effect=slow_boost),
    ):
        step = ParallelEmbedAndEntityBoost()
        # No exception escapes.
        await step.execute(ctx)

    assert ctx.data["embedding"] == _DUMMY_EMBED
    assert ctx.data["boosted_memory_ids"] == set()
    assert ctx.data["memory_boost_factor"] == {}
    # A warning is logged for the dropped entity-boost signal.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING log line on entity-boost fallback"


# ---------------------------------------------------------------------------
# Case 5 — entity boost raises (non-timeout) → graceful fallback
# ---------------------------------------------------------------------------


async def test_entity_boost_exception_falls_back_quietly(monkeypatch, caplog):
    """Any boost exception → empty boost + warning log; embedding kept."""
    _shrink_budgets(monkeypatch)
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    async def fast_embed(*args, **kwargs):
        return _DUMMY_EMBED

    async def kaboom_boost(*args, **kwargs):
        raise RuntimeError("storage-api unreachable")

    ctx = _make_ctx()
    with (
        patch(_EMBED_PATH, side_effect=fast_embed),
        patch(_BOOST_PATH, side_effect=kaboom_boost),
    ):
        step = ParallelEmbedAndEntityBoost()
        await step.execute(ctx)  # must not raise

    assert ctx.data["embedding"] == _DUMMY_EMBED
    assert ctx.data["boosted_memory_ids"] == set()
    assert ctx.data["memory_boost_factor"] == {}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING log line on entity-boost exception"


# ---------------------------------------------------------------------------
# Case 6 — D7 regression: slow entity must NOT cancel a fast embedding
# ---------------------------------------------------------------------------


async def test_d7_slow_entity_does_not_cancel_fast_embedding(monkeypatch, caplog):
    """D7 / PR #260 regression guard.

    The headline behaviour the D7 split-budget fix delivers: a slow entity
    task no longer cancels a fast embedding that already completed.

    Before the fix, both legs shared one ``asyncio.gather(..., wait_for=...)``
    scope, so a 5 s entity-task cancelled the 10 ms embedding and the
    step raised a spurious 504 — even though the embedding succeeded in
    plenty of time.

    After the fix, the embedding has its own ``_OVERALL_TIMEOUT_S`` budget
    and the entity-boost gets its own (smaller) ``_MIN_ENTITY_BUDGET_S``
    budget on the remainder. So this test:

    - mocks embed to finish in ~10 ms
    - mocks boost to sleep 5 s (way over ``_MIN_ENTITY_BUDGET_S``)
    - asserts that the step completes without raising
    - asserts ``ctx.data["embedding"]`` is populated

    If this test ever starts raising HTTPException(504), the D7 split
    has regressed — the two budgets are sharing a single cancellation
    scope again.
    """
    _shrink_budgets(monkeypatch)
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    async def fast_embed(*args, **kwargs):
        await asyncio.sleep(0.01)
        return _DUMMY_EMBED

    async def slow_boost(*args, **kwargs):
        await asyncio.sleep(5.0)
        return (set(), {})

    ctx = _make_ctx()
    with (
        patch(_EMBED_PATH, side_effect=fast_embed),
        patch(_BOOST_PATH, side_effect=slow_boost),
    ):
        step = ParallelEmbedAndEntityBoost()
        # The pre-D7 code would have raised HTTPException(504) here.
        await step.execute(ctx)

    assert ctx.data["embedding"] == _DUMMY_EMBED, (
        "D7 regression: a slow entity task cancelled the fast embedding "
        "(shared timeout scope). See PR #260."
    )
    # Boost legitimately timed out → empty fallback + warning.
    assert ctx.data["boosted_memory_ids"] == set()
    assert ctx.data["memory_boost_factor"] == {}


# ---------------------------------------------------------------------------
# Case 7 — wall-clock ceiling: slow entity cannot blow past total budget
# ---------------------------------------------------------------------------


async def test_wall_clock_bounded_by_overall_timeout(monkeypatch):
    """A slow entity task must not push the step past ``_OVERALL_TIMEOUT_S``."""
    _shrink_budgets(monkeypatch)

    async def fast_embed(*args, **kwargs):
        return _DUMMY_EMBED

    async def slow_boost(*args, **kwargs):
        await asyncio.sleep(5.0)
        return (set(), {})

    ctx = _make_ctx()
    loop = asyncio.get_event_loop()
    with (
        patch(_EMBED_PATH, side_effect=fast_embed),
        patch(_BOOST_PATH, side_effect=slow_boost),
    ):
        step = ParallelEmbedAndEntityBoost()
        t0 = loop.time()
        await step.execute(ctx)
        elapsed = loop.time() - t0

    # Generous slack for asyncio scheduling / pytest overhead, but well
    # under the 5 s slow-boost sleep — proves the entity leg was
    # capped, not awaited to completion.
    slack = 0.5
    ceiling = mod._OVERALL_TIMEOUT_S + slack
    assert elapsed < ceiling, (
        f"step took {elapsed:.3f}s, expected < {ceiling:.3f}s "
        f"(overall budget {mod._OVERALL_TIMEOUT_S}s + slack {slack}s)"
    )
    # Sanity: embedding still made it through.
    assert ctx.data["embedding"] == _DUMMY_EMBED
