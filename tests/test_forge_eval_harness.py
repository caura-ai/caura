"""Forge Phase-1 eval harness (Skill Factory SF-105).

Measures the Phase-1 acceptance gates against three hand-labeled
synthetic fleets:

  * **Stability** — under cluster *growth* (new traces joining an
    existing cluster without moving its center) the fingerprint
    of each pre-existing cluster MUST be unchanged. Plan §8: ≥99%
    stability rate.
  * **Cluster precision** — fraction of emitted-cluster members
    whose ground-truth label matches the cluster's dominant
    label. Plan §15 Phase 1: ≥70%.
  * **Cluster recall** — fraction of ground-truth clusters that
    were detected by Forge (≥50% of their members captured by some
    emitted cluster). Plan §15 Phase 1: ≥60%.

Marked ``@pytest.mark.unit`` because the harness is pure CPU —
no DB, no LLM, no network — deterministic across machines. The
quality measurements are integration-grade *semantically* (they
gate Phase-1 acceptance) but the EXECUTION is pure-unit. Marking
them ``unit`` also lets ``tests/conftest.py``'s autouse DB
fixture skip its setup for this file (it triggers DB connect for
non-unit tests).

Fixture fleets:

  A. "eToro-style devops" — 3 clean, well-separated clusters
     (deploy-eu-west, brand-guidelines, oncall) + 1 noise trace.
  B. "Mixed security/dev with peripheral overlap" — 3 clusters
     where each cluster shares 1 peripheral entity with another
     (the realistic case where strict thresholds matter).
  C. "Variable cluster sizes" — small (3) + medium (6) + large
     (10) clusters in one fleet; tests gate robustness across
     volume regimes.

Add more fixtures here as we observe real-fleet failures during
Phase 1 dry-runs against eToro production.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from core_api.services.forge.forge_service import (
    ForgeConfig,
    _cluster_by_entity_overlap,
    _gate_clusters,
    run_forge_distill,
)
from core_api.services.session_trace import SessionTraceRow


# ── Fleet fixture builder ─────────────────────────────────────────


@dataclass(frozen=True)
class _TraceSpec:
    """Hand-labeled trace spec. ``true_cluster`` is the ground-truth
    cluster id — Forge never sees it; the eval harness uses it to
    score precision/recall."""

    run_id: str
    agent_id: str
    outcome: str  # success | failure | unknown
    entity_ids: tuple[str, ...]
    memory_ids: tuple[str, ...]
    true_cluster: str  # ground-truth label


@dataclass(frozen=True)
class _Fleet:
    name: str
    traces: tuple[_TraceSpec, ...]
    # Mapping ground-truth cluster id → expected min member count.
    # The harness uses this to weight recall.
    expected_clusters: dict[str, int] = field(default_factory=dict)


def _spec_to_row(spec: _TraceSpec) -> SessionTraceRow:
    return SessionTraceRow(
        tenant_id="eval-tenant",
        fleet_id="eval-fleet",
        run_id=spec.run_id,
        agent_id=spec.agent_id,
        outcome_label=spec.outcome,
        memory_ids=list(spec.memory_ids),
        entity_ids=list(spec.entity_ids),
        signals_summary={},
        started_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        ended_at=datetime(2026, 5, 1, 1, tzinfo=timezone.utc),
        goal_phrase=None,
    )


# ── Fleet A: clean separation (3 clusters + 1 noise) ──────────────

_FLEET_A = _Fleet(
    name="A_clean_separation",
    traces=tuple(
        # Cluster: deploy-eu-west (4 traces, 4 distinct agents)
        [
            _TraceSpec(f"run-deploy-{i}", f"agent-deploy-{i}", "success",
                       ("deploy", "eu-west", "step7", "dns"),
                       (f"deploy-m{i}-1", f"deploy-m{i}-2"),
                       "deploy_eu_west")
            for i in range(4)
        ]
        +
        # Cluster: brand-guidelines (3 traces, 3 distinct agents)
        [
            _TraceSpec(f"run-brand-{i}", f"agent-brand-{i}", "success",
                       ("brand", "tokens", "design-system", "typography"),
                       (f"brand-m{i}-1",),
                       "brand_guidelines")
            for i in range(3)
        ]
        +
        # Cluster: oncall (3 traces, 3 distinct agents)
        [
            _TraceSpec(f"run-oncall-{i}", f"agent-oncall-{i}", "success",
                       ("oncall", "alert", "pager", "incident"),
                       (f"oncall-m{i}-1",),
                       "oncall")
            for i in range(3)
        ]
        +
        # Noise: 1 trace with disjoint entities
        [
            _TraceSpec("run-noise", "agent-noise", "success",
                       ("isolated-entity",),
                       ("noise-m1",),
                       "noise")
        ]
    ),
    expected_clusters={"deploy_eu_west": 4, "brand_guidelines": 3, "oncall": 3},
)


# ── Fleet B: peripheral overlap ───────────────────────────────────

_FLEET_B = _Fleet(
    name="B_peripheral_overlap",
    traces=tuple(
        # Cluster: security scan (4 traces) — shares 1 peripheral
        # entity "monitoring" with the next cluster.
        [
            _TraceSpec(f"run-sec-{i}", f"agent-sec-{i}", "success",
                       ("scan", "vuln", "cve", "patch", "monitoring"),
                       (f"sec-m{i}-1",),
                       "security_scan")
            for i in range(4)
        ]
        +
        # Cluster: monitoring-deploy (4 traces) — shares "monitoring" with sec.
        [
            _TraceSpec(f"run-mon-{i}", f"agent-mon-{i}", "success",
                       ("monitoring", "alert", "grafana", "dashboard"),
                       (f"mon-m{i}-1",),
                       "monitoring_deploy")
            for i in range(4)
        ]
        +
        # Cluster: pure-product (3 traces) — fully disjoint.
        [
            _TraceSpec(f"run-prod-{i}", f"agent-prod-{i}", "success",
                       ("ab-test", "experiment", "control", "variant"),
                       (f"prod-m{i}-1",),
                       "product_ab_test")
            for i in range(3)
        ]
    ),
    expected_clusters={
        "security_scan": 4,
        "monitoring_deploy": 4,
        "product_ab_test": 3,
    },
)


# ── Fleet C: variable cluster sizes ───────────────────────────────

_FLEET_C = _Fleet(
    name="C_variable_sizes",
    traces=tuple(
        # Small: 3 traces, 3 agents
        [
            _TraceSpec(f"run-S-{i}", f"agent-S-{i}", "success",
                       ("small-A", "small-B", "small-C"),
                       (f"S-m{i}-1",),
                       "small_cluster")
            for i in range(3)
        ]
        +
        # Medium: 6 traces, 4 agents (one agent appears twice)
        [
            _TraceSpec(f"run-M-{i}", f"agent-M-{min(i, 3)}", "success",
                       ("medium-A", "medium-B", "medium-C", "medium-D"),
                       (f"M-m{i}-1", f"M-m{i}-2"),
                       "medium_cluster")
            for i in range(6)
        ]
        +
        # Large: 10 traces, 5 agents
        [
            _TraceSpec(f"run-L-{i}", f"agent-L-{i % 5}", "success",
                       ("large-A", "large-B", "large-C", "large-D", "large-E"),
                       (f"L-m{i}-1",),
                       "large_cluster")
            for i in range(10)
        ]
    ),
    expected_clusters={
        "small_cluster": 3,
        "medium_cluster": 6,
        "large_cluster": 10,
    },
)


ALL_FLEETS = (_FLEET_A, _FLEET_B, _FLEET_C)


# ── Metric helpers ────────────────────────────────────────────────


def _emitted_clusters(
    fleet: _Fleet,
    *,
    jaccard_threshold: float = 0.4,
) -> list[list[_TraceSpec]]:
    """Run Forge's clustering against the fleet and return the
    cluster groupings as lists of the ORIGINAL TraceSpecs (so we
    can read each trace's ground-truth label later)."""
    rows = [_spec_to_row(s) for s in fleet.traces]
    clusters_of_rows = _cluster_by_entity_overlap(
        rows, jaccard_threshold=jaccard_threshold
    )
    # Build a row.run_id → spec map for label lookup.
    by_run = {s.run_id: s for s in fleet.traces}
    return [[by_run[r.run_id] for r in cluster] for cluster in clusters_of_rows]


def _cluster_precision(emitted: list[list[_TraceSpec]]) -> float:
    """Per emitted cluster, the dominant ground-truth label's
    share. Averaged across clusters."""
    if not emitted:
        return 0.0
    shares: list[float] = []
    for cluster in emitted:
        if not cluster:
            continue
        labels = [t.true_cluster for t in cluster]
        dominant_count = max(labels.count(lbl) for lbl in set(labels))
        shares.append(dominant_count / len(cluster))
    return sum(shares) / len(shares) if shares else 0.0


def _cluster_recall(
    fleet: _Fleet,
    emitted: list[list[_TraceSpec]],
    *,
    capture_threshold: float = 0.5,
) -> float:
    """Fraction of ground-truth clusters where SOME emitted cluster
    captures ≥``capture_threshold`` of its members."""
    detected = 0
    for gt_label, expected_size in fleet.expected_clusters.items():
        if expected_size == 0:
            continue
        max_captured = 0
        for cluster in emitted:
            captured = sum(1 for t in cluster if t.true_cluster == gt_label)
            max_captured = max(max_captured, captured)
        if max_captured / expected_size >= capture_threshold:
            detected += 1
    total = len(fleet.expected_clusters)
    return detected / total if total else 0.0


def _stability_rate(fleet: _Fleet) -> float:
    """Run clustering twice on the same fleet — fingerprints of
    emitted clusters MUST match across runs. Then run a third time
    after adding 1 'extension' trace per cluster (an extra trace
    sharing the cluster's dominant entities) — fingerprints of
    pre-existing clusters MUST stay stable. Returns the fraction of
    runs that agree.
    """
    # Same-input determinism.
    fps_run_1 = _cluster_fingerprints(fleet.traces)
    fps_run_2 = _cluster_fingerprints(fleet.traces)
    same_input_stable = fps_run_1 == fps_run_2

    # Cluster-growth stability: add a single trace per cluster that
    # carries the cluster's dominant entities — should join the
    # existing cluster and NOT move its top-K entity set.
    grown_traces = list(fleet.traces)
    for gt_label in fleet.expected_clusters:
        # Find the entities of the cluster's first member.
        first = next(t for t in fleet.traces if t.true_cluster == gt_label)
        grown_traces.append(
            _TraceSpec(
                run_id=f"{gt_label}-extension-trace",
                agent_id=f"{gt_label}-extension-agent",
                outcome="success",
                entity_ids=first.entity_ids,
                memory_ids=(f"{gt_label}-ext-m1",),
                true_cluster=gt_label,
            )
        )
    fps_run_3 = _cluster_fingerprints(tuple(grown_traces))

    # Stability across the 3 runs: count fingerprints that survive
    # all three executions divided by the count seen in run 1.
    surviving = fps_run_1 & fps_run_2 & fps_run_3
    base = max(len(fps_run_1), 1)
    rate = len(surviving) / base
    # Same-input failures (fps_run_1 != fps_run_2) are catastrophic
    # determinism bugs — treat as 0% if they happen.
    return rate if same_input_stable else 0.0


def _cluster_fingerprints(traces: tuple[_TraceSpec, ...]) -> set[str]:
    """Compute the canonical fingerprint *per cluster* against the
    given traces.

    Because the SF-104 distill step is the one that fills the
    goal_phrase / step_skeleton (LLM-derived) before the
    fingerprint is computed, the eval harness shortcuts by using a
    deterministic in-process substitute: the SORTED tuple of
    cluster member entities + the cluster's dominant true_cluster
    label. This is a *proxy* for the real fingerprint — it
    measures whether the CLUSTER MEMBERSHIP is stable, which is
    the precondition for fingerprint stability.

    The real LLM fingerprint stability is validated in SF-103
    invariants P1–P6; this layer validates that the upstream
    clustering doesn't shuffle members between runs.
    """
    rows = [_spec_to_row(s) for s in traces]
    clusters = _cluster_by_entity_overlap(rows, jaccard_threshold=0.4)
    out: set[str] = set()
    by_run = {s.run_id: s for s in traces}
    for cluster in clusters:
        ents = sorted({e for r in cluster for e in r.entity_ids})
        labels = [by_run[r.run_id].true_cluster for r in cluster]
        dominant = max(set(labels), key=labels.count)
        # Proxy fingerprint: dominant label + entity set.
        out.add(f"{dominant}::{','.join(ents)}")
    return out


# ── Phase-1 acceptance gates ──────────────────────────────────────


@pytest.mark.unit
class TestPhase1AcceptanceGates:
    """The three gates Phase-1 acceptance check ships with."""

    @pytest.mark.parametrize("fleet", ALL_FLEETS, ids=lambda f: f.name)
    def test_stability_at_least_99pct(self, fleet: _Fleet):
        rate = _stability_rate(fleet)
        assert rate >= 0.99, (
            f"{fleet.name}: stability {rate:.3f} < 0.99 — clustering shuffles "
            f"under cluster growth"
        )

    @pytest.mark.parametrize("fleet", ALL_FLEETS, ids=lambda f: f.name)
    def test_cluster_precision_at_least_70pct(self, fleet: _Fleet):
        emitted = _emitted_clusters(fleet)
        precision = _cluster_precision(emitted)
        assert precision >= 0.70, (
            f"{fleet.name}: precision {precision:.3f} < 0.70 — emitted "
            f"clusters mix ground-truth labels"
        )

    @pytest.mark.parametrize("fleet", ALL_FLEETS, ids=lambda f: f.name)
    def test_cluster_recall_at_least_60pct(self, fleet: _Fleet):
        emitted = _emitted_clusters(fleet)
        recall = _cluster_recall(fleet, emitted)
        assert recall >= 0.60, (
            f"{fleet.name}: recall {recall:.3f} < 0.60 — some ground-truth "
            f"clusters not captured"
        )

    @pytest.mark.parametrize("fleet", ALL_FLEETS, ids=lambda f: f.name)
    def test_eligible_clusters_pass_volume_diversity_gates(self, fleet: _Fleet):
        # Sanity: the fixtures we hand-labeled actually satisfy the
        # auto-gates Forge will apply in production
        # (min_cluster_size=3, min_distinct_agents=3).
        rows = [_spec_to_row(s) for s in fleet.traces]
        clusters = _cluster_by_entity_overlap(rows, jaccard_threshold=0.4)
        eligible = _gate_clusters(clusters, min_cluster_size=3, min_distinct_agents=3)
        # Fleet A and B each have 3 well-formed clusters; Fleet C
        # has 3 (small/medium/large) — at least 2 must always make it.
        assert len(eligible) >= 2, (
            f"{fleet.name}: only {len(eligible)} of {len(clusters)} clusters "
            f"survived volume+diversity gates — fixture too sparse"
        )


# ── Aggregate report ──────────────────────────────────────────────


@pytest.mark.unit
class TestPhase1AggregateReport:
    """A summary view across all fleets — emits a single
    human-readable metrics table when run with ``-s``. Useful for
    paste-into-stage-completion-report situations."""

    def test_emit_aggregate_report(self, capsys):
        rows: list[dict[str, Any]] = []
        for fleet in ALL_FLEETS:
            emitted = _emitted_clusters(fleet)
            rows.append({
                "fleet": fleet.name,
                "n_traces": len(fleet.traces),
                "n_emitted_clusters": len(emitted),
                "stability_rate": round(_stability_rate(fleet), 4),
                "cluster_precision": round(_cluster_precision(emitted), 4),
                "cluster_recall": round(_cluster_recall(fleet, emitted), 4),
            })

        # Aggregate.
        overall = {
            "fleets_count": len(rows),
            "min_stability": min(r["stability_rate"] for r in rows),
            "min_precision": min(r["cluster_precision"] for r in rows),
            "min_recall": min(r["cluster_recall"] for r in rows),
        }

        report = {
            "phase_1_acceptance_gates": {
                "stability_target": 0.99,
                "precision_target": 0.70,
                "recall_target": 0.60,
            },
            "per_fleet": rows,
            "aggregate_min": overall,
        }
        print("\n" + json.dumps(report, indent=2))

        # All-fleet gate must hold simultaneously.
        assert overall["min_stability"] >= 0.99, report
        assert overall["min_precision"] >= 0.70, report
        assert overall["min_recall"] >= 0.60, report
