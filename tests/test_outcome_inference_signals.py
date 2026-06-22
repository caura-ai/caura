"""Outcome-inference signal extractor unit tests (Skill Factory SF-101 part 1).

Covers the supersession + contradiction signals — the two simplest of
the six because they read existing memory columns directly with no
new tables required. The remaining four signals (repeat_recall,
terminal_memory, cross_agent_reuse, external_hook) ship in later
substages of SF-101 and get their own tests.

Pure-unit: no DB required. As of Fix 2 Ph5a each signal extractor reads
through the storage client (``get_storage_client().outcome_*_signals(...)``)
rather than ``db.execute``; we patch the relevant client method per module
to return controlled rows (as the storage response dicts) and assert:

  - polarity, weight, memory_ids, details shape
  - the client method is called with the query's tenant / window /
    run_id / agent_id / params
  - default-weight defaults read from the registry
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core_api.services.outcome_inference import (
    DEFAULT_SIGNAL_WEIGHTS,
    Polarity,
    SignalEvidence,
    SignalKind,
    SignalQuery,
)
from core_api.services.outcome_inference import (
    contradictions,
    cross_agent_reuse,
    external_hooks,
    repeat_recall,
    supersessions,
    terminal_memory,
)


# ── Storage-client mock harness ────────────────────────────────────
#
# Each extractor calls ONE storage-client method, named per module. The
# tests patch ``<module>.get_storage_client`` to return a fake whose method
# returns the seeded row dicts (the storage response shape — observed_at as
# an ISO string, ids as strings).

# Map each signal module → the storage-client method it calls.
_CLIENT_METHOD = {
    "contradictions": "outcome_contradiction_signals",
    "supersessions": "outcome_supersession_signals",
    "cross_agent_reuse": "outcome_cross_agent_reuse_signals",
    "terminal_memory": "outcome_terminal_memory_signals",
}


def _patch_signal_client(module, rows: list[dict]):
    """Patch ``<module>.get_storage_client`` so the extractor's read
    returns ``rows``. Returns ``(fake_client, patch_ctx)``; the fake's
    method is an ``AsyncMock`` so the test can assert call args."""
    method_name = _CLIENT_METHOD[module.__name__.rsplit(".", 1)[-1]]
    fake = AsyncMock()
    setattr(fake, method_name, AsyncMock(return_value=rows))
    ctx = patch.object(module, "get_storage_client", return_value=fake)
    return fake, ctx, method_name


def _query(**overrides) -> SignalQuery:
    base = {
        "tenant_id": "t1",
        "fleet_id": None,
        "window_start": datetime(2026, 5, 1, tzinfo=timezone.utc),
        "window_end": datetime(2026, 5, 15, tzinfo=timezone.utc),
        "run_id": None,
        "agent_id": None,
    }
    base.update(overrides)
    return SignalQuery(**base)


# ── SignalEvidence + Enums sanity ─────────────────────────────────


@pytest.mark.unit
class TestSignalTypes:
    def test_signal_kind_values_stable(self):
        # Persisted in session_traces.signals_summary JSONB — these
        # string values must NEVER change without a migration.
        assert SignalKind.CONTRADICTION.value == "contradiction"
        assert SignalKind.SUPERSESSION.value == "supersession"
        assert SignalKind.REPEAT_RECALL.value == "repeat_recall"
        assert SignalKind.TERMINAL_MEMORY.value == "terminal_memory"
        assert SignalKind.CROSS_AGENT_REUSE.value == "cross_agent_reuse"
        assert SignalKind.EXTERNAL_HOOK.value == "external_hook"

    def test_polarity_values_stable(self):
        assert Polarity.SUCCESS.value == "success"
        assert Polarity.FAILURE.value == "failure"
        assert Polarity.NEUTRAL.value == "neutral"

    def test_default_weights_in_range(self):
        # Defensive: all weights in [0, 1].
        for kind, w in DEFAULT_SIGNAL_WEIGHTS.items():
            assert 0.0 <= w <= 1.0, f"{kind.value} weight {w} out of range"

    def test_strong_signals_outweigh_weak(self):
        # Plan §6: terminal-memory + external-hook are the ground-truth
        # signals; cross-agent-reuse is the weakest informational hint.
        assert DEFAULT_SIGNAL_WEIGHTS[SignalKind.TERMINAL_MEMORY] >= 0.85
        assert DEFAULT_SIGNAL_WEIGHTS[SignalKind.EXTERNAL_HOOK] >= 0.8
        assert (
            DEFAULT_SIGNAL_WEIGHTS[SignalKind.CROSS_AGENT_REUSE]
            < DEFAULT_SIGNAL_WEIGHTS[SignalKind.CONTRADICTION]
        )

    def test_evidence_jsonb_shape(self):
        ev = SignalEvidence(
            kind=SignalKind.SUPERSESSION,
            polarity=Polarity.FAILURE,
            weight=0.7,
            memory_ids=("m-1", "m-2"),
            details={"superseded_by_memory_id": "m-3"},
            observed_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        )
        j = ev.as_jsonb()
        assert j["polarity"] == "failure"
        assert j["weight"] == 0.7
        assert j["memory_ids"] == ["m-1", "m-2"]
        assert j["details"] == {"superseded_by_memory_id": "m-3"}
        assert j["observed_at"].startswith("2026-05-10T12:00")


# ── Supersession extractor ────────────────────────────────────────


@pytest.mark.unit
class TestSupersessionExtractor:
    def test_module_exposes_protocol_attrs(self):
        assert supersessions.kind == SignalKind.SUPERSESSION
        assert callable(supersessions.extract)

    @pytest.mark.asyncio
    async def test_no_rows_returns_empty(self):
        fake, ctx, method = _patch_signal_client(supersessions, [])
        with ctx:
            out = await supersessions.extract(_query())
        assert out == []
        # Still issued the read (sanity: not short-circuiting caller-side).
        getattr(fake, method).assert_awaited_once()

    @pytest.mark.asyncio
    async def test_emits_failure_polarity_on_superseded_trace(self):
        rows = [
            {
                "superseded_id": "old-mem-1",
                "run_id": "run-A",
                "agent_id": "sasha",
                "by_id": "new-mem-1",
                "observed_at": "2026-05-05T00:00:00+00:00",
            }
        ]
        _fake, ctx, _ = _patch_signal_client(supersessions, rows)
        with ctx:
            out = await supersessions.extract(_query())
        assert len(out) == 1
        ev = out[0]
        assert ev.kind == SignalKind.SUPERSESSION
        assert ev.polarity == Polarity.FAILURE
        assert ev.weight == DEFAULT_SIGNAL_WEIGHTS[SignalKind.SUPERSESSION]
        # memory_ids points at the SUPERSEDED memory (the bad one), not
        # the corrective one — that's the trace that paid the failure.
        assert ev.memory_ids == ("old-mem-1",)
        assert ev.details["superseded_memory_id"] == "old-mem-1"
        assert ev.details["superseded_by_memory_id"] == "new-mem-1"
        assert ev.details["run_id"] == "run-A"
        assert ev.details["agent_id"] == "sasha"
        assert ev.observed_at == datetime(2026, 5, 5, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_one_evidence_per_supersession(self):
        rows = [
            {"superseded_id": "m1", "run_id": "r1", "agent_id": "a1", "by_id": "n1",
             "observed_at": "2026-05-03T00:00:00+00:00"},
            {"superseded_id": "m2", "run_id": "r2", "agent_id": "a2", "by_id": "n2",
             "observed_at": "2026-05-04T00:00:00+00:00"},
            {"superseded_id": "m3", "run_id": "r1", "agent_id": "a1", "by_id": "n3",
             "observed_at": "2026-05-07T00:00:00+00:00"},
        ]
        _fake, ctx, _ = _patch_signal_client(supersessions, rows)
        with ctx:
            out = await supersessions.extract(_query())
        assert len(out) == 3
        # Each evidence keyed by the superseded memory; same trace can
        # accumulate multiple firings (e.g. an agent that wrote 3 bad
        # claims all gets later contradicted).
        assert {e.memory_ids[0] for e in out} == {"m1", "m2", "m3"}

    @pytest.mark.asyncio
    async def test_passes_query_inputs_to_client(self):
        q = _query(
            tenant_id="acme",
            fleet_id="ops-fleet",
            run_id="r-specific",
            agent_id="alice",
        )
        fake, ctx, method = _patch_signal_client(supersessions, [])
        with ctx:
            await supersessions.extract(q)
        # The extractor threads the query's scope/window into the client read.
        kwargs = getattr(fake, method).await_args.kwargs
        assert kwargs["tenant_id"] == "acme"
        assert kwargs["fleet_id"] == "ops-fleet"
        assert kwargs["run_id"] == "r-specific"
        assert kwargs["agent_id"] == "alice"
        assert kwargs["window_start"] == q.window_start
        assert kwargs["window_end"] == q.window_end


# ── Contradiction extractor ───────────────────────────────────────


@pytest.mark.unit
class TestContradictionExtractor:
    def test_module_exposes_protocol_attrs(self):
        assert contradictions.kind == SignalKind.CONTRADICTION
        assert callable(contradictions.extract)

    def test_contradicted_statuses_match_detector_output(self):
        # The detector writes both "outdated" (RDF path) and
        # "conflicted" (semantic path). Drift between these two sets
        # would silently miss whole categories of contradictions —
        # this assertion catches that at test time.
        assert "outdated" in contradictions.CONTRADICTED_STATUSES
        assert "conflicted" in contradictions.CONTRADICTED_STATUSES

    @pytest.mark.asyncio
    async def test_no_rows_returns_empty(self):
        _fake, ctx, _ = _patch_signal_client(contradictions, [])
        with ctx:
            out = await contradictions.extract(_query())
        assert out == []

    @pytest.mark.asyncio
    async def test_outdated_status_emits_failure(self):
        rows = [
            {
                "memory_id": "mem-1",
                "run_id": "run-X",
                "agent_id": "mira",
                "status": "outdated",
                "observed_at": "2026-05-06T00:00:00+00:00",
            }
        ]
        _fake, ctx, _ = _patch_signal_client(contradictions, rows)
        with ctx:
            out = await contradictions.extract(_query())
        assert len(out) == 1
        ev = out[0]
        assert ev.kind == SignalKind.CONTRADICTION
        assert ev.polarity == Polarity.FAILURE
        assert ev.weight == DEFAULT_SIGNAL_WEIGHTS[SignalKind.CONTRADICTION]
        assert ev.memory_ids == ("mem-1",)
        assert ev.details["memory_id"] == "mem-1"
        assert ev.details["status"] == "outdated"
        assert ev.details["run_id"] == "run-X"
        assert ev.details["agent_id"] == "mira"

    @pytest.mark.asyncio
    async def test_conflicted_status_emits_failure(self):
        rows = [
            {
                "memory_id": "mem-2",
                "run_id": "run-Y",
                "agent_id": "kai",
                "status": "conflicted",
                "observed_at": "2026-05-07T00:00:00+00:00",
            }
        ]
        _fake, ctx, _ = _patch_signal_client(contradictions, rows)
        with ctx:
            out = await contradictions.extract(_query())
        assert out[0].details["status"] == "conflicted"
        assert out[0].polarity == Polarity.FAILURE

    @pytest.mark.asyncio
    async def test_passes_status_array_to_client(self):
        fake, ctx, method = _patch_signal_client(contradictions, [])
        with ctx:
            await contradictions.extract(_query(tenant_id="acme"))
        kwargs = getattr(fake, method).await_args.kwargs
        # The status set is threaded as a list (storage binds it as a
        # text[] for ``= ANY(...)``).
        assert kwargs["contradicted_statuses"] == list(contradictions.CONTRADICTED_STATUSES)
        assert kwargs["tenant_id"] == "acme"

    @pytest.mark.asyncio
    async def test_run_and_agent_filter_pass_through(self):
        fake, ctx, method = _patch_signal_client(contradictions, [])
        with ctx:
            await contradictions.extract(_query(run_id="run-Z", agent_id="alex"))
        kwargs = getattr(fake, method).await_args.kwargs
        assert kwargs["run_id"] == "run-Z"
        assert kwargs["agent_id"] == "alex"

    @pytest.mark.asyncio
    async def test_window_boundaries_passed_to_client(self):
        start = datetime(2026, 4, 1, tzinfo=timezone.utc)
        end = datetime(2026, 4, 15, tzinfo=timezone.utc)
        fake, ctx, method = _patch_signal_client(contradictions, [])
        with ctx:
            await contradictions.extract(_query(window_start=start, window_end=end))
        kwargs = getattr(fake, method).await_args.kwargs
        assert kwargs["window_start"] == start
        assert kwargs["window_end"] == end


# ── Multi-signal independence ─────────────────────────────────────


@pytest.mark.unit
class TestMultiSignalIndependence:
    @pytest.mark.asyncio
    async def test_signals_do_not_share_state(self):
        # Running both extractors against unrelated rows must NOT
        # leak between them.
        sup_rows = [
            {"superseded_id": "s1", "run_id": "r1", "agent_id": "a1", "by_id": "n1",
             "observed_at": "2026-05-05T00:00:00+00:00"}
        ]
        con_rows = [
            {"memory_id": "c1", "run_id": "r2", "agent_id": "a2", "status": "outdated",
             "observed_at": "2026-05-06T00:00:00+00:00"}
        ]
        _f1, ctx1, _ = _patch_signal_client(supersessions, sup_rows)
        with ctx1:
            sup_out = await supersessions.extract(_query())
        _f2, ctx2, _ = _patch_signal_client(contradictions, con_rows)
        with ctx2:
            con_out = await contradictions.extract(_query())
        # Distinct kinds.
        assert sup_out[0].kind != con_out[0].kind
        # Distinct memory_ids surfaced.
        assert sup_out[0].memory_ids != con_out[0].memory_ids
        # Same polarity (both = failure), same weight ceiling for now.
        assert sup_out[0].polarity == con_out[0].polarity == Polarity.FAILURE

    @pytest.mark.asyncio
    async def test_extractors_safe_when_observed_at_none(self):
        # observed_at is nullable per the SignalEvidence schema; both
        # extractors must accept a row with no observed_at without
        # crashing. (Happens when older memories have no recall timestamp.)
        cases = (
            (supersessions, [{"superseded_id": "s", "run_id": "r", "agent_id": "a", "by_id": "n",
                              "observed_at": None}]),
            (contradictions, [{"memory_id": "c", "run_id": "r", "agent_id": "a", "status": "outdated",
                               "observed_at": None}]),
        )
        for mod, rows in cases:
            _fake, ctx, _ = _patch_signal_client(mod, rows)
            with ctx:
                out = await mod.extract(_query())
            assert len(out) == 1
            assert out[0].observed_at is None or isinstance(out[0].observed_at, datetime)


# ── Window degenerate cases ───────────────────────────────────────


@pytest.mark.unit
class TestWindowDegenerateCases:
    @pytest.mark.asyncio
    async def test_empty_window_returns_empty(self):
        # window_start == window_end → storage returns 0 rows; the
        # extractor returns [].
        same = datetime(2026, 5, 10, tzinfo=timezone.utc)
        _fake, ctx, _ = _patch_signal_client(supersessions, [])
        with ctx:
            out = await supersessions.extract(_query(window_start=same, window_end=same))
        assert out == []

    @pytest.mark.asyncio
    async def test_inverted_window_does_not_crash(self):
        # Caller passing window_end < window_start is a programmer
        # error; we don't raise, we just return [] (storage returns 0 rows).
        _fake, ctx, _ = _patch_signal_client(contradictions, [])
        with ctx:
            out = await contradictions.extract(
                _query(
                    window_start=datetime(2026, 5, 10, tzinfo=timezone.utc),
                    window_end=datetime(2026, 5, 1, tzinfo=timezone.utc),
                )
            )
        assert out == []

    @pytest.mark.asyncio
    async def test_wide_window_collects_all(self):
        # A one-year window with many rows must collect every row storage
        # returns — the extractor does NOT impose its own LIMIT.
        many = [
            {"superseded_id": f"m{i}", "run_id": f"r{i}", "agent_id": "a", "by_id": f"n{i}",
             "observed_at": "2026-05-05T00:00:00+00:00"}
            for i in range(50)
        ]
        _fake, ctx, _ = _patch_signal_client(supersessions, many)
        with ctx:
            out = await supersessions.extract(
                _query(
                    window_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    window_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
                )
            )
        assert len(out) == 50


# ── Terminal-memory extractor + classifier ────────────────────────


@pytest.mark.unit
class TestTerminalMemoryClassifier:
    """Direct tests of the classifier function (independent of DB)."""

    def test_success_keyword_shipped(self):
        from core_api.services.outcome_inference.terminal_memory import _classify
        assert _classify("Shipped to prod") == Polarity.SUCCESS

    def test_success_keyword_deployed_phrase(self):
        from core_api.services.outcome_inference.terminal_memory import _classify
        assert _classify("Deployed v4 to eu-west, all green") == Polarity.SUCCESS

    def test_failure_keyword_blocked(self):
        from core_api.services.outcome_inference.terminal_memory import _classify
        assert _classify("Blocked on infra outage, will resume tomorrow") == Polarity.FAILURE

    def test_failure_phrase_gave_up(self):
        from core_api.services.outcome_inference.terminal_memory import _classify
        assert _classify("Gave up after 4 hours of retries") == Polarity.FAILURE

    def test_failure_phrase_rolled_back(self):
        from core_api.services.outcome_inference.terminal_memory import _classify
        assert _classify("Rolled back the change — DB grew too fast") == Polarity.FAILURE

    def test_ambiguous_returns_none(self):
        from core_api.services.outcome_inference.terminal_memory import _classify
        # No keywords from either set.
        assert _classify("Looked at the dashboard, thinking through next steps") is None

    def test_failure_wins_over_success_in_same_text(self):
        # Asymmetric tie-breaking rule from module docstring.
        from core_api.services.outcome_inference.terminal_memory import _classify
        assert _classify("Deployed but rolled back five minutes later") == Polarity.FAILURE

    def test_empty_content_returns_none(self):
        from core_api.services.outcome_inference.terminal_memory import _classify
        assert _classify("") is None
        assert _classify(None) is None  # type: ignore[arg-type]

    def test_word_boundary_avoids_substring_false_positive(self):
        # "preshipped" should NOT fire shipped — \b boundary.
        from core_api.services.outcome_inference.terminal_memory import _classify
        assert _classify("preshipped (early-access feature)") is None


@pytest.mark.unit
class TestTerminalMemoryExtractor:
    def test_module_exposes_protocol_attrs(self):
        assert terminal_memory.kind == SignalKind.TERMINAL_MEMORY
        assert callable(terminal_memory.extract)

    def test_classifier_version_tag_present(self):
        assert terminal_memory.CLASSIFIER_VERSION.startswith("v1-")

    @pytest.mark.asyncio
    async def test_success_terminal_emits_success(self):
        rows = [
            {
                "memory_id": "m1",
                "run_id": "r1",
                "agent_id": "sasha",
                "content": "Shipped ✓ to eu-west",
                "observed_at": "2026-05-10T00:00:00+00:00",
            }
        ]
        _fake, ctx, _ = _patch_signal_client(terminal_memory, rows)
        with ctx:
            out = await terminal_memory.extract(_query())
        assert len(out) == 1
        assert out[0].kind == SignalKind.TERMINAL_MEMORY
        assert out[0].polarity == Polarity.SUCCESS
        assert out[0].weight == DEFAULT_SIGNAL_WEIGHTS[SignalKind.TERMINAL_MEMORY]
        assert out[0].details["classifier_version"] == terminal_memory.CLASSIFIER_VERSION
        assert out[0].details["verdict"] == "success"

    @pytest.mark.asyncio
    async def test_failure_terminal_emits_failure(self):
        rows = [
            {"memory_id": "m1", "run_id": "r1", "agent_id": "kai",
             "content": "Blocked — DNS resolver hangs",
             "observed_at": "2026-05-10T00:00:00+00:00"}
        ]
        _fake, ctx, _ = _patch_signal_client(terminal_memory, rows)
        with ctx:
            out = await terminal_memory.extract(_query())
        assert out[0].polarity == Polarity.FAILURE

    @pytest.mark.asyncio
    async def test_ambiguous_terminal_not_emitted(self):
        # Classifier returns None → no evidence at all.
        rows = [
            {"memory_id": "m1", "run_id": "r1", "agent_id": "a",
             "content": "Looking into it.",
             "observed_at": "2026-05-10T00:00:00+00:00"}
        ]
        _fake, ctx, _ = _patch_signal_client(terminal_memory, rows)
        with ctx:
            out = await terminal_memory.extract(_query())
        assert out == []

    @pytest.mark.asyncio
    async def test_reads_via_terminal_memory_client(self):
        # The DISTINCT ON (run_id, agent_id) terminal-pick now lives
        # storage-side (pinned by the Ph5a storage integration test). Here
        # we confirm the extractor reads via the dedicated client method
        # with the query's scope.
        fake, ctx, method = _patch_signal_client(terminal_memory, [])
        with ctx:
            await terminal_memory.extract(_query(tenant_id="acme"))
        getattr(fake, method).assert_awaited_once()
        assert getattr(fake, method).await_args.kwargs["tenant_id"] == "acme"


# ── Cross-agent-reuse extractor ───────────────────────────────────


@pytest.mark.unit
class TestCrossAgentReuseExtractor:
    def test_module_exposes_protocol_attrs(self):
        assert cross_agent_reuse.kind == SignalKind.CROSS_AGENT_REUSE
        assert callable(cross_agent_reuse.extract)

    def test_threshold_constant_is_conservative(self):
        # Documented in module: 5 total recalls ~ 3 distinct agents.
        # Tighter than 3 to compensate for the inflation by self-recalls.
        assert cross_agent_reuse.DEFAULT_RECALL_COUNT_THRESHOLD >= 3

    @pytest.mark.asyncio
    async def test_no_rows_returns_empty(self):
        _fake, ctx, _ = _patch_signal_client(cross_agent_reuse, [])
        with ctx:
            out = await cross_agent_reuse.extract(_query())
        assert out == []

    @pytest.mark.asyncio
    async def test_load_bearing_emits_neutral(self):
        rows = [
            {
                "memory_id": "m1",
                "run_id": "r1",
                "agent_id": "sasha",
                "recall_count": 8,
                "observed_at": "2026-05-10T00:00:00+00:00",
            }
        ]
        _fake, ctx, _ = _patch_signal_client(cross_agent_reuse, rows)
        with ctx:
            out = await cross_agent_reuse.extract(_query())
        assert len(out) == 1
        # Cross-agent reuse is NEUTRAL — it doesn't claim the
        # originating trace succeeded or failed, only that the
        # artifact is being reused.
        assert out[0].polarity == Polarity.NEUTRAL
        assert out[0].weight == DEFAULT_SIGNAL_WEIGHTS[SignalKind.CROSS_AGENT_REUSE]
        assert out[0].details["recall_count"] == 8
        assert out[0].details["threshold"] == cross_agent_reuse.DEFAULT_RECALL_COUNT_THRESHOLD

    @pytest.mark.asyncio
    async def test_threshold_is_passed_to_client(self):
        fake, ctx, method = _patch_signal_client(cross_agent_reuse, [])
        with ctx:
            await cross_agent_reuse.extract(_query())
        kwargs = getattr(fake, method).await_args.kwargs
        assert kwargs["threshold"] == cross_agent_reuse.DEFAULT_RECALL_COUNT_THRESHOLD


# ── Repeat-recall extractor (MVP stub) ────────────────────────────


@pytest.mark.unit
class TestRepeatRecallStub:
    def test_module_exposes_protocol_attrs(self):
        assert repeat_recall.kind == SignalKind.REPEAT_RECALL
        assert callable(repeat_recall.extract)

    @pytest.mark.asyncio
    async def test_mvp_always_returns_empty(self):
        # MVP: until Phase 2 ships the recall-log table, the
        # extractor returns []. Documented behavior; tests pin it so
        # an accidental flip can't slip through.
        out = await repeat_recall.extract(_query())
        assert out == []

    def test_stub_does_not_import_storage_client(self):
        # The stub MUST stay costless — it imports no storage client, so it
        # can't issue any read until the real implementation lands. Pinning
        # the absence of the symbol guards against an accidental wiring.
        assert not hasattr(repeat_recall, "get_storage_client")


# ── External-hooks extractor (MVP stub) ───────────────────────────


@pytest.mark.unit
class TestExternalHooksStub:
    def test_module_exposes_protocol_attrs(self):
        assert external_hooks.kind == SignalKind.EXTERNAL_HOOK
        assert callable(external_hooks.extract)

    @pytest.mark.asyncio
    async def test_mvp_always_returns_empty(self):
        out = await external_hooks.extract(_query())
        assert out == []

    def test_stub_does_not_import_storage_client(self):
        # Same costless-stub invariant as repeat_recall.
        assert not hasattr(external_hooks, "get_storage_client")


# ── Registry: all 6 signals discoverable + uniquely keyed ────────


@pytest.mark.unit
class TestSignalRegistry:
    """The session-trace builder will discover extractors by
    iterating the package. These tests pin the registry so a
    missing/renamed module shows up at test time rather than as a
    silent missing signal in production."""

    ALL_MODULES = (
        contradictions,
        supersessions,
        terminal_memory,
        cross_agent_reuse,
        repeat_recall,
        external_hooks,
    )

    def test_all_six_signals_exposed(self):
        kinds = {mod.kind for mod in self.ALL_MODULES}
        assert kinds == set(SignalKind), (
            f"signal modules cover {sorted(k.value for k in kinds)}; "
            f"enum lists {sorted(k.value for k in SignalKind)}"
        )

    def test_each_module_exposes_extract_coroutine(self):
        import asyncio
        for mod in self.ALL_MODULES:
            assert asyncio.iscoroutinefunction(mod.extract), (
                f"{mod.__name__}.extract must be a coroutine"
            )

    def test_kinds_are_unique(self):
        kinds = [mod.kind for mod in self.ALL_MODULES]
        assert len(kinds) == len(set(kinds)), "duplicate signal kinds across modules"

    def test_each_module_documents_its_data_source(self):
        # Every signal module's docstring must reference its data
        # source ("memories.", "track_recalls", "memory_recalls", or
        # "external") so operators reading the code at 3 AM know
        # where to look. Catches accidentally undocumented stubs.
        for mod in self.ALL_MODULES:
            doc = (mod.__doc__ or "").lower()
            assert any(
                token in doc
                for token in ("memories.", "track_recalls", "memory_recalls", "external",
                              "audit", "recall_count", "contradiction", "supersedes", "phase 2", "phase 5")
            ), f"{mod.__name__} docstring lacks data-source breadcrumb"
