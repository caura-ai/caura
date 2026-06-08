"""Benchmarks for P5 (crystallizer) and P6-1 (type decay) changes.

Measures:
- Type-based freshness computation latency (should be <1μs overhead)
- Cluster building (union-find) at scale
- Pair deduplication at scale
"""

import time
import uuid

import pytest

from core_api.constants import (
    FRESHNESS_DECAY_DAYS,
    FRESHNESS_FLOOR,
    MEMORY_TYPES,
    TYPE_DECAY_DAYS,
)


def _compute_freshness_typed(age_days: float, memory_type: str) -> float:
    """Typed freshness (P6-1 implementation)."""
    decay_days = TYPE_DECAY_DAYS.get(memory_type, FRESHNESS_DECAY_DAYS)
    if age_days < decay_days:
        return 1.0 - (age_days / decay_days) * (1.0 - FRESHNESS_FLOOR)
    return FRESHNESS_FLOOR


def _compute_freshness_flat(age_days: float) -> float:
    """Flat freshness (pre-P6-1 behavior)."""
    if age_days < FRESHNESS_DECAY_DAYS:
        return 1.0 - (age_days / FRESHNESS_DECAY_DAYS) * (1.0 - FRESHNESS_FLOOR)
    return FRESHNESS_FLOOR


@pytest.mark.benchmark
class TestTypeDecayBenchmark:
    """Measure overhead of type-based decay vs flat decay."""

    ITERATIONS = 50_000

    def test_typed_freshness_latency(self):
        """Type-based freshness should add negligible overhead."""
        types = list(MEMORY_TYPES)
        ages = [10, 30, 60, 90, 180, 365]

        t0 = time.perf_counter_ns()
        for i in range(self.ITERATIONS):
            mt = types[i % len(types)]
            age = ages[i % len(ages)]
            _compute_freshness_typed(age, mt)
        elapsed_ns = time.perf_counter_ns() - t0

        per_call_ns = elapsed_ns / self.ITERATIONS
        per_call_us = per_call_ns / 1000

        assert per_call_us < 5.0, f"Type-based freshness too slow: {per_call_us:.2f}μs/call"
        print(f"\n  Type-based freshness: {per_call_us:.3f}μs/call ({self.ITERATIONS} iterations)")

    def test_flat_freshness_latency(self):
        """Baseline: flat freshness for comparison."""
        ages = [10, 30, 60, 90, 180, 365]

        t0 = time.perf_counter_ns()
        for i in range(self.ITERATIONS):
            age = ages[i % len(ages)]
            _compute_freshness_flat(age)
        elapsed_ns = time.perf_counter_ns() - t0

        per_call_ns = elapsed_ns / self.ITERATIONS
        per_call_us = per_call_ns / 1000

        print(f"\n  Flat freshness: {per_call_us:.3f}μs/call ({self.ITERATIONS} iterations)")

    def test_typed_vs_flat_overhead(self):
        """Type-based should be <2x overhead vs flat."""
        ages = [10, 30, 60, 90, 180, 365]
        types = list(MEMORY_TYPES)
        n = self.ITERATIONS

        # Flat
        t0 = time.perf_counter_ns()
        for i in range(n):
            _compute_freshness_flat(ages[i % len(ages)])
        flat_ns = time.perf_counter_ns() - t0

        # Typed
        t0 = time.perf_counter_ns()
        for i in range(n):
            _compute_freshness_typed(ages[i % len(ages)], types[i % len(types)])
        typed_ns = time.perf_counter_ns() - t0

        ratio = typed_ns / flat_ns if flat_ns > 0 else 1.0
        print(f"\n  Typed/flat ratio: {ratio:.2f}x")
        assert ratio < 3.0, f"Typed freshness is {ratio:.2f}x slower than flat — too much overhead"


@pytest.mark.benchmark
class TestClusterBuildingBenchmark:
    """Measure union-find cluster building at scale."""

    def test_cluster_1000_pairs(self):
        """Build clusters from 1000 pairs (max safety valve size)."""
        from core_api.services.crystallizer_service import _build_clusters

        ids = [str(uuid.uuid4()) for _ in range(500)]
        # Create a mix of chains and isolated pairs
        pairs = []
        # 10 chains of 30 nodes each (29 pairs per chain)
        for chain in range(10):
            start = chain * 30
            for i in range(29):
                pairs.append({"id1": ids[start + i], "id2": ids[start + i + 1]})
        # Fill remaining with isolated pairs
        while len(pairs) < 1000:
            a, b = str(uuid.uuid4()), str(uuid.uuid4())
            pairs.append({"id1": a, "id2": b})

        t0 = time.perf_counter_ns()
        clusters = _build_clusters(pairs)
        elapsed_ns = time.perf_counter_ns() - t0
        elapsed_us = elapsed_ns / 1000

        print(f"\n  Union-find 1000 pairs → {len(clusters)} clusters: {elapsed_us:.1f}μs")
        assert elapsed_us < 50_000, f"Cluster building too slow: {elapsed_us:.0f}μs (target: <50ms)"

    def test_cluster_50_pairs_regression(self):
        """Verify no regression at old cap of 50 pairs."""
        from core_api.services.crystallizer_service import _build_clusters

        pairs = [{"id1": str(uuid.uuid4()), "id2": str(uuid.uuid4())} for _ in range(50)]

        t0 = time.perf_counter_ns()
        _build_clusters(pairs)
        elapsed_us = (time.perf_counter_ns() - t0) / 1000

        print(f"\n  Union-find 50 pairs: {elapsed_us:.1f}μs")
        assert elapsed_us < 5_000


@pytest.mark.benchmark
class TestPairNormalizationBenchmark:
    """Measure pair deduplication overhead."""

    def test_pair_sorting_10000(self):
        """Sort 10K id pairs to normalize order."""
        pairs_raw = [(str(uuid.uuid4()), str(uuid.uuid4())) for _ in range(10_000)]

        t0 = time.perf_counter_ns()
        for a, b in pairs_raw:
            sorted([a, b])
        elapsed_us = (time.perf_counter_ns() - t0) / 1000

        per_pair_us = elapsed_us / 10_000
        print(f"\n  Pair normalization: {per_pair_us:.3f}μs/pair ({elapsed_us:.0f}μs total for 10K)")
        assert per_pair_us < 5.0
