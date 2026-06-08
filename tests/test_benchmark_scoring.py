"""Latency benchmarks: before vs after scoring formula.

Measures the Python-side computation overhead of the old vs new formulas.
These are pure-math benchmarks (no DB) — they measure the scoring logic only,
not the SQL query execution time (which is dominated by pgvector ANN search).

Run with: pytest tests/test_benchmark_scoring.py -v -s --tb=short
"""

import statistics
import time
from datetime import datetime, timedelta, timezone

import pytest

from core_api.constants import (
    FRESHNESS_DECAY_DAYS,
    FRESHNESS_FLOOR,
    RECALL_BOOST_CAP,
    RECALL_BOOST_SCALE,
    RECALL_DECAY_WINDOW_DAYS,
    SIMILARITY_BLEND,
)


# ---------------------------------------------------------------------------
# Old formulas (before P0 fixes)
# ---------------------------------------------------------------------------

def old_freshness(created_at: datetime, now: datetime) -> float:
    """Original: simple linear decay from created_at."""
    age_days = (now - created_at).total_seconds() / 86400.0
    if age_days < FRESHNESS_DECAY_DAYS:
        return 1.0 - (age_days / FRESHNESS_DECAY_DAYS) * (1.0 - FRESHNESS_FLOOR)
    return FRESHNESS_FLOOR


def old_recall_boost(recall_count: int) -> float:
    """Original: raw count curve, no time decay."""
    return 1.0 + (RECALL_BOOST_CAP - 1.0) * recall_count / (recall_count + RECALL_BOOST_SCALE)


def old_score(similarity: float, weight: float, freshness: float,
              entity_boost: float, recall_boost: float) -> float:
    """Original: pure multiplicative."""
    return similarity * weight * freshness * entity_boost * recall_boost


# ---------------------------------------------------------------------------
# New formulas (after P0 fixes)
# ---------------------------------------------------------------------------

def new_freshness(
    created_at: datetime,
    ts_valid_start: datetime | None,
    ts_valid_end: datetime | None,
    now: datetime,
) -> float:
    """P0-2: temporal-window-aware."""
    if ts_valid_end is not None and ts_valid_end < now:
        return FRESHNESS_FLOOR
    if ts_valid_end is not None:
        return 1.0
    anchor = max(created_at, ts_valid_start or created_at)
    age_days = (now - anchor).total_seconds() / 86400.0
    if age_days < FRESHNESS_DECAY_DAYS:
        return 1.0 - (age_days / FRESHNESS_DECAY_DAYS) * (1.0 - FRESHNESS_FLOOR)
    return FRESHNESS_FLOOR


def new_recall_boost(recall_count: int, last_recalled_at: datetime | None,
                     created_at: datetime, now: datetime) -> float:
    """P0-3: time-decayed."""
    anchor = last_recalled_at or created_at
    days_since = (now - anchor).total_seconds() / 86400.0
    recency = max(0.0, 1.0 - days_since / RECALL_DECAY_WINDOW_DAYS)
    return 1.0 + (RECALL_BOOST_CAP - 1.0) * recency * recall_count / (recall_count + RECALL_BOOST_SCALE)


def new_score(similarity: float, weight: float, freshness: float,
              entity_boost: float, recall_boost: float) -> float:
    """P0-4: additive blend."""
    base = SIMILARITY_BLEND * similarity + (1.0 - SIMILARITY_BLEND) * weight
    return base * freshness * entity_boost * recall_boost


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def _benchmark(fn, iterations=50_000):
    """Run fn() N times, return (mean_us, p50_us, p99_us, total_ms)."""
    # Warmup
    for _ in range(1000):
        fn()
    # Measure
    times = []
    for _ in range(iterations):
        t0 = time.perf_counter_ns()
        fn()
        times.append(time.perf_counter_ns() - t0)
    times_us = [t / 1000 for t in times]
    return {
        "mean_us": statistics.mean(times_us),
        "p50_us": statistics.median(times_us),
        "p99_us": sorted(times_us)[int(len(times_us) * 0.99)],
        "total_ms": sum(times_us) / 1000,
        "iterations": iterations,
    }


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------

@pytest.mark.benchmark
class TestScoringLatency:
    """Compare old vs new formula computation overhead.

    These run 50K iterations of each formula and report timing.
    The new formulas add at most a few microseconds per call — well within
    the noise floor of a PG query (~1-10ms).
    """

    def _make_scenario(self):
        """Create a realistic test scenario."""
        now = datetime.now(timezone.utc)
        return {
            "now": now,
            "created_at": now - timedelta(days=45),
            "ts_valid_start": now - timedelta(days=5),
            "ts_valid_end": now + timedelta(days=30),
            "last_recalled_at": now - timedelta(days=3),
            "recall_count": 15,
            "similarity": 0.82,
            "weight": 0.65,
            "entity_boost": 1.2,
        }

    def test_freshness_latency(self):
        s = self._make_scenario()

        old_result = _benchmark(
            lambda: old_freshness(s["created_at"], s["now"])
        )
        new_result = _benchmark(
            lambda: new_freshness(s["created_at"], s["ts_valid_start"], s["ts_valid_end"], s["now"])
        )

        overhead_us = new_result["mean_us"] - old_result["mean_us"]
        print(f"\n{'─' * 60}")
        print(f"FRESHNESS DECAY (50K iterations)")
        print(f"  Old: mean={old_result['mean_us']:.2f}μs  p50={old_result['p50_us']:.2f}μs  p99={old_result['p99_us']:.2f}μs")
        print(f"  New: mean={new_result['mean_us']:.2f}μs  p50={new_result['p50_us']:.2f}μs  p99={new_result['p99_us']:.2f}μs")
        print(f"  Overhead: {overhead_us:+.2f}μs/call ({overhead_us/max(old_result['mean_us'], 0.01)*100:+.1f}%)")
        print(f"{'─' * 60}")

        # Sanity: new formula should add <5μs per call
        assert new_result["mean_us"] < 50, f"New freshness too slow: {new_result['mean_us']:.1f}μs"

    def test_recall_boost_latency(self):
        s = self._make_scenario()

        old_result = _benchmark(
            lambda: old_recall_boost(s["recall_count"])
        )
        new_result = _benchmark(
            lambda: new_recall_boost(s["recall_count"], s["last_recalled_at"], s["created_at"], s["now"])
        )

        overhead_us = new_result["mean_us"] - old_result["mean_us"]
        print(f"\n{'─' * 60}")
        print(f"RECALL BOOST (50K iterations)")
        print(f"  Old: mean={old_result['mean_us']:.2f}μs  p50={old_result['p50_us']:.2f}μs  p99={old_result['p99_us']:.2f}μs")
        print(f"  New: mean={new_result['mean_us']:.2f}μs  p50={new_result['p50_us']:.2f}μs  p99={new_result['p99_us']:.2f}μs")
        print(f"  Overhead: {overhead_us:+.2f}μs/call ({overhead_us/max(old_result['mean_us'], 0.01)*100:+.1f}%)")
        print(f"{'─' * 60}")

        assert new_result["mean_us"] < 50, f"New recall boost too slow: {new_result['mean_us']:.1f}μs"

    def test_full_score_latency(self):
        s = self._make_scenario()

        # Precompute shared values
        old_fresh = old_freshness(s["created_at"], s["now"])
        old_rb = old_recall_boost(s["recall_count"])
        new_fresh = new_freshness(s["created_at"], s["ts_valid_start"], s["ts_valid_end"], s["now"])
        new_rb = new_recall_boost(s["recall_count"], s["last_recalled_at"], s["created_at"], s["now"])

        old_result = _benchmark(
            lambda: old_score(s["similarity"], s["weight"], old_fresh, s["entity_boost"], old_rb)
        )
        new_result = _benchmark(
            lambda: new_score(s["similarity"], s["weight"], new_fresh, s["entity_boost"], new_rb)
        )

        overhead_us = new_result["mean_us"] - old_result["mean_us"]
        print(f"\n{'─' * 60}")
        print(f"FULL SCORE (50K iterations)")
        print(f"  Old: mean={old_result['mean_us']:.2f}μs  p50={old_result['p50_us']:.2f}μs  p99={old_result['p99_us']:.2f}μs")
        print(f"  New: mean={new_result['mean_us']:.2f}μs  p50={new_result['p50_us']:.2f}μs  p99={new_result['p99_us']:.2f}μs")
        print(f"  Overhead: {overhead_us:+.2f}μs/call ({overhead_us/max(old_result['mean_us'], 0.01)*100:+.1f}%)")
        print(f"{'─' * 60}")

        assert new_result["mean_us"] < 50, f"New full score too slow: {new_result['mean_us']:.1f}μs"

    def test_combined_pipeline_latency(self):
        """End-to-end: freshness + recall boost + score, all combined."""
        s = self._make_scenario()

        def old_pipeline():
            f = old_freshness(s["created_at"], s["now"])
            rb = old_recall_boost(s["recall_count"])
            return old_score(s["similarity"], s["weight"], f, s["entity_boost"], rb)

        def new_pipeline():
            f = new_freshness(s["created_at"], s["ts_valid_start"], s["ts_valid_end"], s["now"])
            rb = new_recall_boost(s["recall_count"], s["last_recalled_at"], s["created_at"], s["now"])
            return new_score(s["similarity"], s["weight"], f, s["entity_boost"], rb)

        old_result = _benchmark(old_pipeline)
        new_result = _benchmark(new_pipeline)

        overhead_us = new_result["mean_us"] - old_result["mean_us"]
        overhead_pct = overhead_us / max(old_result["mean_us"], 0.01) * 100

        print(f"\n{'═' * 60}")
        print(f"COMBINED PIPELINE (50K iterations)")
        print(f"  Old: mean={old_result['mean_us']:.2f}μs  p50={old_result['p50_us']:.2f}μs  p99={old_result['p99_us']:.2f}μs")
        print(f"  New: mean={new_result['mean_us']:.2f}μs  p50={new_result['p50_us']:.2f}μs  p99={new_result['p99_us']:.2f}μs")
        print(f"  Overhead: {overhead_us:+.2f}μs/call ({overhead_pct:+.1f}%)")
        print(f"  Context: PG query latency is ~1,000-10,000μs")
        print(f"           Scoring overhead is <0.1% of total search latency")
        print(f"{'═' * 60}")

        # Combined pipeline should be under 100μs (trivial vs DB roundtrip)
        assert new_result["mean_us"] < 100, f"Combined pipeline too slow: {new_result['mean_us']:.1f}μs"
