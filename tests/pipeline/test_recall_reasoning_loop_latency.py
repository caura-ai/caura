"""Latency guard: reasoning_mode=False adds no overhead to the default search path (RL-04).

Confirms the new opt-in `reasoning_mode` parameter on search_memories() is free
when unused: calling without the param at all (today's default call shape) and
calling with `reasoning_mode=False` explicitly must produce statistically
indistinguishable latency, since both take the exact same code path —
GraphReasoningBoost.execute short-circuits to StepOutcome.SKIPPED before
touching the LLM whenever reasoning_mode is falsy.

reasoning_mode=True is measured separately and logged only, never gated on —
multi-turn LLM tool-selection round-trips are inherently slower and are not
part of the default-path latency guarantee this test protects.

Requires a running PostgreSQL instance (integration test).
"""

import statistics
import time
import uuid

import pytest

from core_api.schemas import MemoryCreate, MemoryOut

ITERATIONS = 10

FLEET_ID = "test-fleet"
AGENT_ID = "test-agent"

_SEED_CONTENTS = [
    "The quick brown fox jumped over the lazy dog on a sunny afternoon in the park near downtown.",
    "Alice prefers dark roast coffee every morning before her standup meeting at nine o'clock sharp.",
    "The quarterly budget review is scheduled for next Friday with the entire finance department attending.",
    "Bob mentioned he is allergic to peanuts and tree nuts, which is important for team lunch orders.",
    "The new deployment pipeline uses GitHub Actions with staging and production environments configured.",
]


async def _seed_memories(db, tenant_id: str, count: int = 5) -> None:
    """Insert seed memories via the write path so the search arms have real rows to score."""
    from core_api.services.memory_service import create_memory

    for i in range(min(count, len(_SEED_CONTENTS))):
        await create_memory(
            MemoryCreate(
                tenant_id=tenant_id,
                fleet_id=FLEET_ID,
                agent_id=AGENT_ID,
                content=_SEED_CONTENTS[i],
                persist=True,
                entity_links=[],
            )
        )


# Ratio-based tolerance, consistent with test_pipeline_latency.py's rationale:
# absolute-ms budgets flake on noisy CI runners. The invariant pinned here is
# "the unused reasoning_mode branch adds no measurable overhead relative to
# not passing the param at all" — not an absolute millisecond figure.
_REASONING_MODE_FALSE_OVERHEAD_RATIO_MAX = 1.10


@pytest.mark.asyncio
async def test_reasoning_mode_false_matches_default_latency(db):
    """reasoning_mode=False (explicit) p50 stays within ~10% of the no-param baseline."""
    import logging

    from core_api.services import memory_service
    from core_api.services.memory_service import search_memories

    runner_logger = logging.getLogger("core_api.pipeline.runner")
    prev_level = runner_logger.level
    runner_logger.setLevel(logging.WARNING)

    original_use_pipeline = memory_service._USE_PIPELINE_SEARCH
    memory_service._USE_PIPELINE_SEARCH = True

    tenant_id = f"test-rl04-lat-{uuid.uuid4().hex[:8]}"

    try:
        await _seed_memories(db, tenant_id)

        # ── Warmup (prime imports, caches, connections) ──
        warmup = await search_memories(
            tenant_id=tenant_id,
            query="quick brown fox",
            fleet_ids=[FLEET_ID],
            caller_agent_id=AGENT_ID,
        )
        assert len(warmup) > 0, "seed memories did not come back from search — arms below would be measuring nothing"

        # ── Measure: no reasoning_mode kwarg at all (today's default call shape) ──
        baseline_latencies = []
        for _ in range(ITERATIONS):
            t0 = time.perf_counter()
            results = await search_memories(
                tenant_id=tenant_id,
                query="quick brown fox",
                fleet_ids=[FLEET_ID],
                caller_agent_id=AGENT_ID,
            )
            baseline_latencies.append((time.perf_counter() - t0) * 1000)
            assert all(isinstance(r, MemoryOut) for r in results)

        # ── Measure: reasoning_mode=False passed explicitly ──
        explicit_false_latencies = []
        for _ in range(ITERATIONS):
            t0 = time.perf_counter()
            results = await search_memories(
                tenant_id=tenant_id,
                query="quick brown fox",
                fleet_ids=[FLEET_ID],
                caller_agent_id=AGENT_ID,
                reasoning_mode=False,
            )
            explicit_false_latencies.append((time.perf_counter() - t0) * 1000)
            assert all(isinstance(r, MemoryOut) for r in results)

        baseline_median = statistics.median(baseline_latencies)
        explicit_false_median = statistics.median(explicit_false_latencies)
        ratio = explicit_false_median / baseline_median if baseline_median > 0 else float("inf")

        print(f"\n{'=' * 60}")
        print("REASONING_MODE=False OVERHEAD CHECK")
        print(f"{'=' * 60}")
        print(
            f"No-param baseline    — median: {baseline_median:.2f}ms  "
            f"p95: {_percentile(baseline_latencies, 95):.2f}ms"
        )
        print(
            f"reasoning_mode=False — median: {explicit_false_median:.2f}ms  "
            f"p95: {_percentile(explicit_false_latencies, 95):.2f}ms"
        )
        print(f"Ratio — {ratio:.3f}x")
        print(f"{'=' * 60}")

        assert explicit_false_median <= baseline_median * _REASONING_MODE_FALSE_OVERHEAD_RATIO_MAX, (
            f"reasoning_mode=False median {explicit_false_median:.2f}ms is {ratio:.2f}x the "
            f"no-param baseline {baseline_median:.2f}ms, exceeds "
            f"{_REASONING_MODE_FALSE_OVERHEAD_RATIO_MAX}x ceiling."
        )

        # ── Measure (do NOT gate): reasoning_mode=True ──
        # Multi-turn LLM tool-selection round-trips are inherently slower; this
        # is logged for visibility only, never asserted against the default path.
        reasoning_true_latencies = []
        for _ in range(ITERATIONS):
            t0 = time.perf_counter()
            results = await search_memories(
                tenant_id=tenant_id,
                query="quick brown fox",
                fleet_ids=[FLEET_ID],
                caller_agent_id=AGENT_ID,
                reasoning_mode=True,
            )
            reasoning_true_latencies.append((time.perf_counter() - t0) * 1000)
            assert isinstance(results, list)

        reasoning_true_median = statistics.median(reasoning_true_latencies)
        print(
            f"reasoning_mode=True (not gated) — median: {reasoning_true_median:.2f}ms  "
            f"p95: {_percentile(reasoning_true_latencies, 95):.2f}ms"
        )
        print(f"{'=' * 60}")

    finally:
        memory_service._USE_PIPELINE_SEARCH = original_use_pipeline
        runner_logger.setLevel(prev_level)


def _percentile(data: list[float], pct: float) -> float:
    """Simple percentile calculation."""
    sorted_data = sorted(data)
    idx = (pct / 100) * (len(sorted_data) - 1)
    lower = int(idx)
    upper = lower + 1
    if upper >= len(sorted_data):
        return sorted_data[-1]
    frac = idx - lower
    return sorted_data[lower] * (1 - frac) + sorted_data[upper] * frac
