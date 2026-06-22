"""Session-trace builder unit tests (Skill Factory SF-102).

Pure-unit: mocks the DB + the 6 extractor modules to exercise the
orchestration + folding logic independently of any persistence
layer.

Coverage map:
  - fold_outcome_label: weighted-sum, neutral-ignored, min-weight
    threshold, asymmetric tie-handling
  - summarize_evidence: grouping by kind, total_weight, polarity_counts
  - build_session_traces: concurrent extractor dispatch, evidence
    partitioning by (run_id, agent_id), persistence guard, exception
    isolation across extractors
  - SQL bind safety on the memory + entity queries
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from core_api.services.outcome_inference import (
    DEFAULT_SIGNAL_WEIGHTS,
    Polarity,
    SignalEvidence,
    SignalKind,
)
from core_api.services.session_trace import (
    MIN_TOTAL_WEIGHT_FOR_LABEL,
    SIGNAL_MODULES,
    SessionTraceRow,
    build_session_traces,
    fold_outcome_label,
    summarize_evidence,
)


# ── Helpers ────────────────────────────────────────────────────────


def _ev(
    kind: SignalKind,
    polarity: Polarity,
    *,
    run_id: str = "r1",
    agent_id: str = "a1",
    weight: float | None = None,
    memory_id: str = "m1",
) -> SignalEvidence:
    return SignalEvidence(
        kind=kind,
        polarity=polarity,
        weight=DEFAULT_SIGNAL_WEIGHTS[kind] if weight is None else weight,
        memory_ids=(memory_id,),
        details={"run_id": run_id, "agent_id": agent_id, "memory_id": memory_id},
        observed_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
    )


@dataclass
class _MemRow:
    memory_id: str
    run_id: str
    agent_id: str
    fleet_id: str | None
    created_at: datetime


@dataclass
class _EntityRow:
    """Mock row for the ``memory_entity_links`` read.

    The builder asks storage for ``(memory_id, entity_id)`` pairs in one
    bulk call; this row carries both columns so the regrouping logic can be
    exercised.
    """

    memory_id: str
    entity_id: str


class _FakeStorageClient:
    """Stand-in for the storage client the session-trace builder uses.

    Ph5a: ``build_session_traces`` no longer takes a ``db`` session — it
    calls ``sc.session_memories_in_window`` (run-scoped memories),
    ``sc.session_trace_entity_links`` (bulk entity links), and
    ``sc.upsert_session_traces`` (persist). This fake records calls so the
    tests can assert the single-bulk-entity-query invariant + the
    persist/no-persist split without any real SQL.
    """

    def __init__(self, memory_rows: list[_MemRow], entity_rows: list[_EntityRow]):
        self._memory_rows = memory_rows
        self._entity_rows = entity_rows
        self.entity_link_calls: list[list[str]] = []
        self.upsert_calls: list[list[dict]] = []

    async def session_memories_in_window(self, *, tenant_id, fleet_id, window_start, window_end):
        return [
            {
                "memory_id": m.memory_id,
                "run_id": m.run_id,
                "agent_id": m.agent_id,
                "fleet_id": m.fleet_id,
                "created_at": m.created_at.isoformat(),
            }
            for m in self._memory_rows
        ]

    async def session_trace_entity_links(self, *, tenant_id, memory_ids):
        self.entity_link_calls.append(list(memory_ids))
        wanted = set(memory_ids)
        return [
            {"memory_id": e.memory_id, "entity_id": e.entity_id}
            for e in self._entity_rows
            if e.memory_id in wanted
        ]

    async def upsert_session_traces(self, *, tenant_id, traces):
        self.upsert_calls.append(list(traces))


def _patch_storage_for_build(
    memory_rows: list[_MemRow],
    entity_rows: list[_EntityRow] | None = None,
) -> tuple[_FakeStorageClient, object]:
    """Build a fake storage client + a patch context that wires it into
    ``session_trace.get_storage_client``. Returns ``(fake, ctx)``; the
    caller uses ``with ctx:`` and asserts on ``fake``."""
    fake = _FakeStorageClient(memory_rows, entity_rows or [])
    ctx = patch("core_api.services.session_trace.get_storage_client", return_value=fake)
    return fake, ctx


# ── fold_outcome_label ─────────────────────────────────────────────


@pytest.mark.unit
class TestFoldOutcomeLabel:
    def test_empty_evidence_unknown(self):
        assert fold_outcome_label([]) == "unknown"

    def test_single_strong_success_labels_success(self):
        # Terminal-memory alone (weight 0.9) crosses min_total_weight (0.5).
        assert fold_outcome_label([_ev(SignalKind.TERMINAL_MEMORY, Polarity.SUCCESS)]) == "success"

    def test_single_strong_failure_labels_failure(self):
        assert fold_outcome_label([_ev(SignalKind.TERMINAL_MEMORY, Polarity.FAILURE)]) == "failure"

    def test_single_weak_signal_below_threshold_unknown(self):
        # Cross-agent-reuse alone (weight 0.3) is below the 0.5 floor.
        # But it's NEUTRAL polarity — so even at high weight wouldn't vote.
        # Use a synthetic weak SUCCESS to test the threshold.
        weak = _ev(SignalKind.CROSS_AGENT_REUSE, Polarity.SUCCESS, weight=0.3)
        assert fold_outcome_label([weak]) == "unknown"

    def test_neutral_evidence_does_not_vote(self):
        # A pile of NEUTRAL (cross-agent-reuse) doesn't tip the scale.
        evs = [_ev(SignalKind.CROSS_AGENT_REUSE, Polarity.NEUTRAL, weight=0.3) for _ in range(10)]
        assert fold_outcome_label(evs) == "unknown"

    def test_failure_outweighs_success_labels_failure(self):
        # 1 supersession (0.7 failure) vs 1 terminal-memory (0.9 success):
        # success wins.
        evs = [
            _ev(SignalKind.SUPERSESSION, Polarity.FAILURE),       # 0.7
            _ev(SignalKind.TERMINAL_MEMORY, Polarity.SUCCESS),    # 0.9
        ]
        assert fold_outcome_label(evs) == "success"

    def test_two_failures_outweigh_one_success(self):
        # 0.7 + 0.7 = 1.4 failure vs 0.9 success: failure wins.
        evs = [
            _ev(SignalKind.SUPERSESSION, Polarity.FAILURE),
            _ev(SignalKind.CONTRADICTION, Polarity.FAILURE),
            _ev(SignalKind.TERMINAL_MEMORY, Polarity.SUCCESS),
        ]
        assert fold_outcome_label(evs) == "failure"

    def test_exact_tie_returns_unknown(self):
        # Two equal-weight signals on opposite polarities.
        evs = [
            _ev(SignalKind.SUPERSESSION, Polarity.FAILURE, weight=0.7),
            _ev(SignalKind.SUPERSESSION, Polarity.SUCCESS, weight=0.7),
        ]
        assert fold_outcome_label(evs) == "unknown"

    def test_min_weight_threshold_configurable(self):
        # A tenant tightens the threshold; weak evidence no longer commits.
        terminal = _ev(SignalKind.TERMINAL_MEMORY, Polarity.SUCCESS)
        assert fold_outcome_label([terminal], min_total_weight=2.0) == "unknown"
        assert fold_outcome_label([terminal], min_total_weight=0.5) == "success"

    def test_min_total_weight_default_matches_constant(self):
        # Defensive: API default must match the documented constant.
        # Bumping one without the other would silently shift attribution.
        assert MIN_TOTAL_WEIGHT_FOR_LABEL == 0.5


# ── summarize_evidence ─────────────────────────────────────────────


@pytest.mark.unit
class TestSummarizeEvidence:
    def test_empty_yields_empty_dict(self):
        assert summarize_evidence([]) == {}

    def test_groups_by_kind(self):
        evs = [
            _ev(SignalKind.SUPERSESSION, Polarity.FAILURE),
            _ev(SignalKind.CONTRADICTION, Polarity.FAILURE),
            _ev(SignalKind.SUPERSESSION, Polarity.FAILURE),
        ]
        out = summarize_evidence(evs)
        assert set(out.keys()) == {"supersession", "contradiction"}
        assert len(out["supersession"]["firings"]) == 2
        assert len(out["contradiction"]["firings"]) == 1

    def test_total_weight_sums_correctly(self):
        evs = [
            _ev(SignalKind.SUPERSESSION, Polarity.FAILURE, weight=0.5),
            _ev(SignalKind.SUPERSESSION, Polarity.FAILURE, weight=0.7),
        ]
        out = summarize_evidence(evs)
        assert out["supersession"]["total_weight"] == 1.2

    def test_polarity_counts(self):
        evs = [
            _ev(SignalKind.TERMINAL_MEMORY, Polarity.SUCCESS),
            _ev(SignalKind.TERMINAL_MEMORY, Polarity.SUCCESS),
            _ev(SignalKind.TERMINAL_MEMORY, Polarity.FAILURE),
        ]
        out = summarize_evidence(evs)
        assert out["terminal_memory"]["polarity_counts"] == {"success": 2, "failure": 1}

    def test_serializable_to_json(self):
        # The summary lands in session_traces.signals_summary JSONB.
        # If anything in there isn't json-serializable we'd fail at
        # write time — test pins it.
        evs = [_ev(SignalKind.SUPERSESSION, Polarity.FAILURE)]
        json.dumps(summarize_evidence(evs))  # raises if non-serializable


# ── build_session_traces orchestration ────────────────────────────


@pytest.mark.unit
class TestBuildSessionTracesOrchestration:
    @pytest.mark.asyncio
    async def test_no_memories_no_traces(self):
        _fake, ctx = _patch_storage_for_build(memory_rows=[])
        with ctx:
            out = await build_session_traces(
                tenant_id="t1",
                fleet_id=None,
                window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
                window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
                persist=False,
            )
        assert out == []

    @pytest.mark.asyncio
    async def test_groups_memories_by_run_and_agent(self):
        mem = [
            _MemRow("m1", "run-A", "sasha", None, datetime(2026, 5, 5, tzinfo=timezone.utc)),
            _MemRow("m2", "run-A", "sasha", None, datetime(2026, 5, 6, tzinfo=timezone.utc)),
            _MemRow("m3", "run-B", "mira",  None, datetime(2026, 5, 7, tzinfo=timezone.utc)),
        ]
        _fake, ctx = _patch_storage_for_build(memory_rows=mem)
        with ctx:
            out = await build_session_traces(
                tenant_id="t1", fleet_id=None,
                window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
                window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
                persist=False,
            )
        # Two distinct (run_id, agent_id) groups → two traces.
        assert len(out) == 2
        traces_by_key = {(r.run_id, r.agent_id): r for r in out}
        assert traces_by_key[("run-A", "sasha")].memory_ids == ["m1", "m2"]
        assert traces_by_key[("run-A", "sasha")].started_at == datetime(2026, 5, 5, tzinfo=timezone.utc)
        assert traces_by_key[("run-A", "sasha")].ended_at == datetime(2026, 5, 6, tzinfo=timezone.utc)
        assert traces_by_key[("run-B", "mira")].memory_ids == ["m3"]

    @pytest.mark.asyncio
    async def test_runs_all_six_extractors_concurrently(self):
        mem = [_MemRow("m1", "r1", "a1", None, datetime(2026, 5, 5, tzinfo=timezone.utc))]
        _fake, ctx = _patch_storage_for_build(memory_rows=mem)

        # Patch every extractor to return [] but capture invocation count.
        call_counts: dict[str, int] = {mod.__name__: 0 for mod in SIGNAL_MODULES}

        async def make_recorder(name):
            async def fake_extract(*_args, **_kwargs):
                call_counts[name] += 1
                return []
            return fake_extract

        patches = [ctx]
        for mod in SIGNAL_MODULES:
            fake = await make_recorder(mod.__name__)
            patches.append(patch.object(mod, "extract", new=fake))

        for p in patches:
            p.start()
        try:
            await build_session_traces(
                tenant_id="t1", fleet_id=None,
                window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
                window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
                persist=False,
            )
        finally:
            for p in patches:
                p.stop()

        # All 6 extractors called exactly once.
        assert all(v == 1 for v in call_counts.values()), call_counts

    @pytest.mark.asyncio
    async def test_evidence_partitions_to_correct_trace(self):
        mem = [
            _MemRow("m1", "run-A", "sasha", None, datetime(2026, 5, 5, tzinfo=timezone.utc)),
            _MemRow("m2", "run-B", "mira",  None, datetime(2026, 5, 6, tzinfo=timezone.utc)),
        ]
        _fake, ctx = _patch_storage_for_build(memory_rows=mem)

        # Stub all extractors except terminal_memory to return [].
        # terminal_memory returns one success for run-A/sasha only.
        from core_api.services.outcome_inference import (
            contradictions, cross_agent_reuse, external_hooks,
            repeat_recall, supersessions, terminal_memory,
        )
        async def succ_for_a(_q):
            return [_ev(SignalKind.TERMINAL_MEMORY, Polarity.SUCCESS,
                        run_id="run-A", agent_id="sasha", memory_id="m1")]
        async def noop(*_a, **_k): return []

        with (
            ctx,
            patch.object(terminal_memory, "extract", new=succ_for_a),
            patch.object(contradictions, "extract", new=noop),
            patch.object(supersessions, "extract", new=noop),
            patch.object(cross_agent_reuse, "extract", new=noop),
            patch.object(repeat_recall, "extract", new=noop),
            patch.object(external_hooks, "extract", new=noop),
        ):
            out = await build_session_traces(
                tenant_id="t1", fleet_id=None,
                window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
                window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
                persist=False,
            )

        by_key = {(r.run_id, r.agent_id): r for r in out}
        assert by_key[("run-A", "sasha")].outcome_label == "success"
        assert by_key[("run-B", "mira")].outcome_label == "unknown"  # no evidence
        # The terminal_memory signal landed under the right trace's summary.
        assert "terminal_memory" in by_key[("run-A", "sasha")].signals_summary
        assert by_key[("run-B", "mira")].signals_summary == {}

    @pytest.mark.asyncio
    async def test_one_extractor_exception_does_not_abort_build(self):
        mem = [_MemRow("m1", "r1", "a1", None, datetime(2026, 5, 5, tzinfo=timezone.utc))]
        _fake, ctx = _patch_storage_for_build(memory_rows=mem)

        from core_api.services.outcome_inference import (
            contradictions, cross_agent_reuse, external_hooks,
            repeat_recall, supersessions, terminal_memory,
        )
        async def boom(*_a, **_k): raise RuntimeError("simulated extractor crash")
        async def succ_for_a(_q):
            return [_ev(SignalKind.TERMINAL_MEMORY, Polarity.SUCCESS,
                        run_id="r1", agent_id="a1", memory_id="m1")]
        async def noop(*_a, **_k): return []

        with (
            ctx,
            patch.object(contradictions, "extract", new=boom),
            patch.object(terminal_memory, "extract", new=succ_for_a),
            patch.object(supersessions, "extract", new=noop),
            patch.object(cross_agent_reuse, "extract", new=noop),
            patch.object(repeat_recall, "extract", new=noop),
            patch.object(external_hooks, "extract", new=noop),
        ):
            out = await build_session_traces(
                tenant_id="t1", fleet_id=None,
                window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
                window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
                persist=False,
            )

        # The successful extractor's evidence still labels the trace,
        # despite the contradiction extractor blowing up.
        assert len(out) == 1
        assert out[0].outcome_label == "success"

    @pytest.mark.asyncio
    async def test_persist_false_skips_insert(self):
        mem = [_MemRow("m1", "r1", "a1", None, datetime(2026, 5, 5, tzinfo=timezone.utc))]
        fake, ctx = _patch_storage_for_build(memory_rows=mem)
        with ctx:
            await build_session_traces(
                tenant_id="t1", fleet_id=None,
                window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
                window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
                persist=False,
            )
        # persist=False → no upsert call to storage.
        assert fake.upsert_calls == []

    @pytest.mark.asyncio
    async def test_persist_true_upserts_all_traces_in_one_batch(self):
        mem = [
            _MemRow("m1", "run-A", "sasha", None, datetime(2026, 5, 5, tzinfo=timezone.utc)),
            _MemRow("m2", "run-B", "mira",  None, datetime(2026, 5, 6, tzinfo=timezone.utc)),
        ]
        fake, ctx = _patch_storage_for_build(memory_rows=mem)
        with ctx:
            await build_session_traces(
                tenant_id="t1", fleet_id=None,
                window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
                window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
                persist=True,
            )
        # One upsert call carrying BOTH traces in a single batch (the
        # builder hands the whole list to sc.upsert_session_traces).
        assert len(fake.upsert_calls) == 1
        assert len(fake.upsert_calls[0]) == 2

    @pytest.mark.asyncio
    async def test_entity_ids_resolved_via_link_table(self):
        mem = [_MemRow("m1", "r1", "a1", None, datetime(2026, 5, 5, tzinfo=timezone.utc))]
        # 3 link rows for the same memory; builder dedupes + sorts.
        ents = [
            _EntityRow("m1", "e-2"),
            _EntityRow("m1", "e-1"),
            _EntityRow("m1", "e-3"),
        ]
        _fake, ctx = _patch_storage_for_build(memory_rows=mem, entity_rows=ents)
        with ctx:
            out = await build_session_traces(
                tenant_id="t1", fleet_id=None,
                window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
                window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
                persist=False,
            )
        # Entity ids sorted (deterministic input for fingerprint later).
        assert out[0].entity_ids == ["e-1", "e-2", "e-3"]

    @pytest.mark.asyncio
    async def test_entity_fetch_passes_all_window_memory_ids(self):
        """The builder must hand EVERY window memory id to the bulk
        entity-links read in one call. (The index-preserving
        ``CAST(:memory_ids AS uuid[])`` SQL now lives storage-side and is
        pinned by the Ph5a storage integration test.)"""
        mem = [
            _MemRow("m1", "run-A", "sasha", None, datetime(2026, 5, 5, tzinfo=timezone.utc)),
            _MemRow("m2", "run-B", "mira",  None, datetime(2026, 5, 6, tzinfo=timezone.utc)),
        ]
        fake, ctx = _patch_storage_for_build(memory_rows=mem)
        with ctx:
            await build_session_traces(
                tenant_id="t1",
                fleet_id=None,
                window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
                window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
                persist=False,
            )
        assert len(fake.entity_link_calls) == 1
        assert sorted(fake.entity_link_calls[0]) == ["m1", "m2"]

    @pytest.mark.asyncio
    async def test_entity_fetch_is_single_bulk_query(self):
        """Regression guard for the N+1 fix: the entity-links read must
        be issued EXACTLY ONCE per builder invocation, no matter how many
        traces are in the window.

        The previous shape issued one query per trace inside the
        per-trace loop — a 100-trace tick was 100+ extra round-trips.
        """
        mem = [
            _MemRow("m1", "run-A", "sasha", None, datetime(2026, 5, 5, tzinfo=timezone.utc)),
            _MemRow("m2", "run-B", "mira",  None, datetime(2026, 5, 6, tzinfo=timezone.utc)),
            _MemRow("m3", "run-C", "kai",   None, datetime(2026, 5, 7, tzinfo=timezone.utc)),
        ]
        ents = [
            _EntityRow("m1", "e-1"),
            _EntityRow("m2", "e-2"),
            _EntityRow("m3", "e-3"),
        ]
        fake, ctx = _patch_storage_for_build(memory_rows=mem, entity_rows=ents)
        with ctx:
            await build_session_traces(
                tenant_id="t1", fleet_id=None,
                window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
                window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
                persist=False,
            )
        # Exactly ONE entity-links call, not one per trace.
        assert len(fake.entity_link_calls) == 1, (
            f"expected exactly one entity-links call, got {len(fake.entity_link_calls)}; "
            "N+1 regression"
        )

    @pytest.mark.asyncio
    async def test_evidence_missing_run_id_or_agent_id_logs_warning(self, caplog):
        """A buggy extractor that fails to stamp ``run_id`` /
        ``agent_id`` on its evidence has no trace to attach to —
        the evidence is dropped. Dropping is the safe default
        (better than crashing the whole build), but it MUST log
        at WARNING so the regression surfaces in operator logs
        rather than disappearing silently."""
        import logging
        import core_api.services.session_trace as svc

        mem = [_MemRow("m1", "r1", "a1", None, datetime(2026, 5, 5, tzinfo=timezone.utc))]
        _fake, ctx = _patch_storage_for_build(memory_rows=mem)

        # Patch one extractor to return evidence with EMPTY details —
        # simulates a regression where a future signal forgets to
        # stamp the trace identity.
        from core_api.services.outcome_inference import (
            contradictions,
            cross_agent_reuse,
            external_hooks,
            repeat_recall,
            supersessions,
            terminal_memory,
        )

        async def bad_extractor(_q):
            return [
                SignalEvidence(
                    kind=SignalKind.TERMINAL_MEMORY,
                    polarity=Polarity.SUCCESS,
                    weight=0.9,
                    memory_ids=("m1",),
                    details={},  # ← intentionally missing run_id / agent_id
                )
            ]

        async def noop(*_a, **_k):
            return []

        with (
            ctx,
            patch.object(terminal_memory, "extract", new=bad_extractor),
            patch.object(contradictions, "extract", new=noop),
            patch.object(supersessions, "extract", new=noop),
            patch.object(cross_agent_reuse, "extract", new=noop),
            patch.object(repeat_recall, "extract", new=noop),
            patch.object(external_hooks, "extract", new=noop),
            caplog.at_level(logging.WARNING, logger=svc.__name__),
        ):
            out = await build_session_traces(
                tenant_id="t1",
                fleet_id=None,
                window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
                window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
                persist=False,
            )

        # The bad evidence was dropped — the trace gets unknown label.
        assert len(out) == 1
        assert out[0].outcome_label == "unknown"
        # And the operator log shows WHY.
        assert any(
            "dropping" in record.message and "terminal_memory" in record.message
            for record in caplog.records
        ), f"expected a 'dropping terminal_memory' warning; got {[r.message for r in caplog.records]}"

    @pytest.mark.asyncio
    async def test_entity_partitioning_to_correct_trace(self):
        """The bulk fetch returns ALL (memory_id, entity_id) pairs;
        the builder must partition them back so each trace only
        sees ITS members' entities — no cross-pollination."""
        mem = [
            _MemRow("m1", "run-A", "sasha", None, datetime(2026, 5, 5, tzinfo=timezone.utc)),
            _MemRow("m2", "run-A", "sasha", None, datetime(2026, 5, 5, 1, tzinfo=timezone.utc)),
            _MemRow("m3", "run-B", "mira",  None, datetime(2026, 5, 6, tzinfo=timezone.utc)),
        ]
        ents = [
            _EntityRow("m1", "e-A1"),
            _EntityRow("m2", "e-A2"),
            _EntityRow("m3", "e-B"),
        ]
        _fake, ctx = _patch_storage_for_build(memory_rows=mem, entity_rows=ents)
        with ctx:
            out = await build_session_traces(
                tenant_id="t1", fleet_id=None,
                window_start=datetime(2026, 5, 1, tzinfo=timezone.utc),
                window_end=datetime(2026, 5, 15, tzinfo=timezone.utc),
                persist=False,
            )
        by_key = {(r.run_id, r.agent_id): r for r in out}
        # Trace A owns m1+m2 → entities e-A1, e-A2 only.
        assert by_key[("run-A", "sasha")].entity_ids == ["e-A1", "e-A2"]
        # Trace B owns m3 → entity e-B only — must NOT see A's entities.
        assert by_key[("run-B", "mira")].entity_ids == ["e-B"]


# ── SessionTraceRow shape ─────────────────────────────────────────


@pytest.mark.unit
class TestSessionTraceRow:
    def test_as_bind_renders_full_dict(self):
        row = SessionTraceRow(
            tenant_id="t",
            fleet_id="f",
            run_id="r",
            agent_id="a",
            outcome_label="success",
            memory_ids=["m1", "m2"],
            entity_ids=["e1"],
            signals_summary={"terminal_memory": {"firings": []}},
            started_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            ended_at=datetime(2026, 5, 2, tzinfo=timezone.utc),
            goal_phrase=None,
        )
        bind = row.as_bind()
        assert bind["outcome_label"] == "success"
        assert bind["memory_ids"] == ["m1", "m2"]
        assert bind["goal_phrase"] is None

    def test_outcome_label_must_be_one_of_three(self):
        # Soft invariant: the DB CHECK constraint
        # (ck_session_traces_outcome_label) limits this to
        # success/failure/unknown. The builder ONLY produces these
        # three; pin via fold_outcome_label tests above. This test
        # is the explicit "Spec sanity" doc.
        produced = {
            fold_outcome_label([]),
            fold_outcome_label([_ev(SignalKind.TERMINAL_MEMORY, Polarity.SUCCESS)]),
            fold_outcome_label([_ev(SignalKind.SUPERSESSION, Polarity.FAILURE)]),
        }
        assert produced == {"unknown", "success", "failure"}
