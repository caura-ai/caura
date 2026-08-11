"""Latency comparison: pipeline path vs legacy path.

Runs both paths with identical workloads and asserts:
1. Pipeline framework adds bounded overhead per write (≤50% over legacy)
2. Both paths write identical memory fields to the DB
3. All writes actually land in PostgreSQL

Requires a running PostgreSQL instance (integration test).
"""

import statistics
import time
import uuid

import pytest
from sqlalchemy import func, select

from common.models.memory import Memory
from core_api.schemas import MemoryCreate, MemoryOut

ITERATIONS = 10

# Two separate tenants so both paths can write the same content
# without triggering cross-path dedup
# ``test-tenant-`` prefix deliberately: conftest's session teardown sweeps on
# exactly that pattern, and these rows are committed through the service layer
# so nothing else removes them. Under the old ``test-lat-`` prefix every run
# leaked 22 rows permanently — 4,334 had accumulated in one dev database.
LEGACY_TENANT = f"test-tenant-lat-legacy-{uuid.uuid4().hex[:8]}"
PIPELINE_TENANT = f"test-tenant-lat-pipe-{uuid.uuid4().hex[:8]}"
FLEET_ID = "test-fleet"
AGENT_ID = "test-agent"


def _make_content(idx: int) -> str:
    """Deterministic per-index content. Same idx → same content for both paths."""
    # Use a fixed seed per index so content is reproducible but unique across indices
    # 4 "words" derived from index to give fake_embedding enough differentiation
    words = [f"word{idx * 4 + j}xyz{idx}" for j in range(4)]
    return (
        f"Memory number {idx} about {' '.join(words)} — "
        f"this content exercises the full write path for latency benchmarking."
    )


def _make_input(tenant_id: str, idx: int, **kwargs) -> MemoryCreate:
    return MemoryCreate(
        tenant_id=tenant_id,
        fleet_id=FLEET_ID,
        agent_id=AGENT_ID,
        content=_make_content(idx),
        persist=True,
        entity_links=[],
        **kwargs,
    )


# ── Fields that must be identical between legacy and pipeline rows ──
_COMPARED_FIELDS = [
    "tenant_id",
    "fleet_id",
    "agent_id",
    "memory_type",
    "title",
    "content",
    "weight",
    "source_uri",
    "run_id",
    "content_hash",
    "subject_entity_id",
    "predicate",
    "object_value",
    "ts_valid_start",
    "ts_valid_end",
    "status",
    "visibility",
]

# ── Metadata keys that must match (excluding timing/latency fields) ──
_COMPARED_META_KEYS = [
    "summary",
    "tags",
    "contains_pii",
    "pii_types",
    "embedding_pending",
]
# Timing-dependent metadata (both paths should populate, but values differ):
# write_latency_ms, semantic_dedup_ms, llm_ms


# Allow the pipeline path's median latency to be up to 1.5x the legacy path's.
# The threshold is unchanged; what changed is the measurement, which is what
# actually flaked (1.545x on 2026-08-09, passing on rerun).
#
# The paths used to be timed in separate blocks — ten legacy writes, then ten
# pipeline writes. Whichever block ran second met the worse conditions, both
# from runner drift and from cold caches after the first block's work, so the
# penalty always landed on pipeline. Measured over 20 paired replicates
# locally: the block shape's ratio has a CV of 10.5% and reached 1.403 on an
# idle laptop, while interleaving the paths halves it to 4.9% with a worst
# case of 1.175. Widening the ratio would not have fixed that — the bias is in
# the ordering, not the number.
#
# Median, not minimum: min looks like the noise-robust choice but is not, here.
# Latency climbs ~28% from the first write to the tenth because the dedup query
# scans the tenant's rows rather than an ANN index, so per-write cost grows with
# rows already written. min therefore lands on the first iteration (argmin was
# i=0 in 15 of 20 replicates) and measures the smallest candidate set rather
# than the least noise. Measured, it also buys nothing once interleaved — CV
# 4.50% vs the median's 4.71% across 10 cross-process runs — while going blind
# to the regressions most likely here: a per-candidate cost added to dedup went
# undetected 100% of the time against min, and 90-100% detected against the
# median.
#
# Residual known bias, and it is the safe direction: within each pair the second
# write measures ~3.8pp slower, and pipeline runs second, so the ratio is if
# anything overstated.
_PIPELINE_OVERHEAD_RATIO_MAX = 1.5


@pytest.mark.asyncio
async def test_pipeline_overhead_bounded_relative_to_legacy(db):
    """Pipeline path median ≤ 1.5× legacy median, writes equivalent rows."""
    import logging

    from core_api.services import memory_service
    from core_api.services.memory_service import create_memory

    # Suppress the runner's per-step INFO lines for the duration of this test.
    # The runner now emits structured ``extra={...}`` dicts (CAURA-602) that
    # legitimately cost ~200us per step — measurable, but orthogonal to what
    # this test is guarding (pipeline-framework glue overhead). The runner's
    # ``isEnabledFor(INFO)`` guard skips the dict build when INFO is filtered.
    runner_logger = logging.getLogger("core_api.pipeline.runner")
    prev_level = runner_logger.level
    runner_logger.setLevel(logging.WARNING)

    original = memory_service._USE_PIPELINE_WRITE

    try:
        # ── Warmup (prime imports, caches, connections) ──
        memory_service._USE_PIPELINE_WRITE = False
        await create_memory(MemoryCreate(
                tenant_id=LEGACY_TENANT,
                fleet_id=FLEET_ID,
                agent_id=AGENT_ID,
                content=f"warmup {uuid.uuid4().hex} — enough content for the quality gate check.",
                persist=True,
                entity_links=[],
            ),
        )
        memory_service._USE_PIPELINE_WRITE = True
        await create_memory(MemoryCreate(
                tenant_id=PIPELINE_TENANT,
                fleet_id=FLEET_ID,
                agent_id=AGENT_ID,
                content=f"warmup {uuid.uuid4().hex} — enough content for the quality gate check.",
                persist=True,
                entity_links=[],
            ),
        )

        # ── Measure both paths, interleaved ──
        # One write of each per iteration, not all of one path then all of the
        # other: see _PIPELINE_OVERHEAD_RATIO_MAX for why the ordering was the
        # bug. ``data`` is built by the caller so MemoryCreate construction
        # stays outside the timed region, as it always has.
        async def timed_write(use_pipeline: bool, data: MemoryCreate) -> float:
            memory_service._USE_PIPELINE_WRITE = use_pipeline
            t0 = time.perf_counter()
            result = await create_memory(data)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            assert isinstance(result, MemoryOut)
            return elapsed_ms

        legacy_latencies = []
        pipeline_latencies = []
        for i in range(ITERATIONS):
            legacy_latencies.append(
                await timed_write(False, _make_input(LEGACY_TENANT, i))
            )
            pipeline_latencies.append(
                # strong mode so both paths run semantic dedup
                await timed_write(
                    True, _make_input(PIPELINE_TENANT, i, write_mode="strong")
                )
            )

        # ── DB row count verification ──
        for tenant, label in [(LEGACY_TENANT, "legacy"), (PIPELINE_TENANT, "pipeline")]:
            count = (
                await db.execute(
                    select(func.count())
                    .select_from(Memory)
                    .where(Memory.tenant_id == tenant)
                )
            ).scalar()
            expected = 1 + ITERATIONS  # 1 warmup + ITERATIONS
            print(f"\n{label}: {count} rows in DB (expected {expected})")
            assert count == expected, f"{label}: expected {expected} rows, got {count}"

        # ── Field-by-field comparison of DB rows ──
        # Query the ITERATIONS memories (exclude warmup) ordered by content
        # so index i matches between tenants
        legacy_rows = (
            (
                await db.execute(
                    select(Memory)
                    .where(
                        Memory.tenant_id == LEGACY_TENANT,
                        Memory.content.like("Memory number %"),
                    )
                    .order_by(Memory.content)
                )
            )
            .scalars()
            .all()
        )

        pipeline_rows = (
            (
                await db.execute(
                    select(Memory)
                    .where(
                        Memory.tenant_id == PIPELINE_TENANT,
                        Memory.content.like("Memory number %"),
                    )
                    .order_by(Memory.content)
                )
            )
            .scalars()
            .all()
        )

        assert len(legacy_rows) == ITERATIONS, f"legacy: got {len(legacy_rows)} rows"
        assert len(pipeline_rows) == ITERATIONS, (
            f"pipeline: got {len(pipeline_rows)} rows"
        )

        mismatches = []
        for i, (leg, pip) in enumerate(zip(legacy_rows, pipeline_rows)):
            for field in _COMPARED_FIELDS:
                leg_val = getattr(leg, field)
                pip_val = getattr(pip, field)
                # tenant_id intentionally differs
                if field == "tenant_id":
                    continue
                # content_hash differs because tenant_id is part of the hash
                if field == "content_hash":
                    # Both should be non-None
                    assert leg_val is not None, f"row {i}: legacy content_hash is None"
                    assert pip_val is not None, (
                        f"row {i}: pipeline content_hash is None"
                    )
                    continue
                if leg_val != pip_val:
                    mismatches.append(
                        f"row {i} field '{field}': legacy={leg_val!r} vs pipeline={pip_val!r}"
                    )

            # Compare embeddings: both should be non-None and identical
            # (same content → same fake_embedding output)
            assert leg.embedding is not None, f"row {i}: legacy embedding is None"
            assert pip.embedding is not None, f"row {i}: pipeline embedding is None"
            assert list(leg.embedding) == list(pip.embedding), (
                f"row {i}: embeddings differ"
            )

            # Compare metadata (excluding timing fields)
            leg_meta = leg.metadata_ or {}
            pip_meta = pip.metadata_ or {}
            for key in _COMPARED_META_KEYS:
                if leg_meta.get(key) != pip_meta.get(key):
                    mismatches.append(
                        f"row {i} metadata['{key}']: "
                        f"legacy={leg_meta.get(key)!r} vs pipeline={pip_meta.get(key)!r}"
                    )

            # Timing metadata: values differ but both paths must populate them
            for timing_key in ["semantic_dedup_ms", "write_latency_ms"]:
                leg_has = timing_key in leg_meta
                pip_has = timing_key in pip_meta
                if leg_has != pip_has:
                    mismatches.append(
                        f"row {i} metadata['{timing_key}'] presence: "
                        f"legacy={leg_has} vs pipeline={pip_has}"
                    )

        if mismatches:
            print(f"\nFIELD MISMATCHES ({len(mismatches)}):")
            for m in mismatches:
                print(f"  - {m}")
        assert not mismatches, f"{len(mismatches)} field mismatches found"

        print(
            f"\nField comparison: {ITERATIONS} row pairs × {len(_COMPARED_FIELDS)} fields — ALL MATCH"
        )

        # ── Latency report ──
        legacy_median = statistics.median(legacy_latencies)
        pipeline_median = statistics.median(pipeline_latencies)
        legacy_min = min(legacy_latencies)
        pipeline_min = min(pipeline_latencies)
        overhead = pipeline_median - legacy_median

        print(f"\n{'=' * 60}")
        print(f"LATENCY COMPARISON ({ITERATIONS} interleaved iterations each)")
        print(f"{'=' * 60}")
        print(
            f"Legacy   — median: {legacy_median:.1f}ms  "
            f"p95: {_percentile(legacy_latencies, 95):.1f}ms  "
            f"min: {legacy_min:.1f}ms  "
            f"max: {max(legacy_latencies):.1f}ms"
        )
        print(
            f"Pipeline — median: {pipeline_median:.1f}ms  "
            f"p95: {_percentile(pipeline_latencies, 95):.1f}ms  "
            f"min: {pipeline_min:.1f}ms  "
            f"max: {max(pipeline_latencies):.1f}ms"
        )
        print(f"Overhead — {overhead:+.1f}ms (median)")
        median_ratio = (
            pipeline_median / legacy_median if legacy_median > 0 else float("inf")
        )
        min_ratio = pipeline_min / legacy_min if legacy_min > 0 else float("inf")
        print(f"Ratio    — median {median_ratio:.2f}x (gate), min {min_ratio:.2f}x")
        print(f"{'=' * 60}")

        assert pipeline_median <= legacy_median * _PIPELINE_OVERHEAD_RATIO_MAX, (
            f"Pipeline median {pipeline_median:.1f}ms is "
            f"{median_ratio:.2f}x legacy median {legacy_median:.1f}ms, exceeds "
            f"{_PIPELINE_OVERHEAD_RATIO_MAX}x ceiling (overhead={overhead:+.1f}ms)."
        )

    finally:
        memory_service._USE_PIPELINE_WRITE = original
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
