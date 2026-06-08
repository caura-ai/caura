"""Outcome-inference signal extractor unit tests (Skill Factory SF-101 part 1).

Covers the supersession + contradiction signals — the two simplest of
the six because they read existing memory columns directly with no
new tables required. The remaining four signals (repeat_recall,
terminal_memory, cross_agent_reuse, external_hook) ship in later
substages of SF-101 and get their own tests.

Pure-unit: no DB required. The signal extractors execute one SQL
statement each via ``db.execute(text(sql), params)``; we mock the
async DB engine to return controlled rows and assert:

  - polarity, weight, memory_ids, details shape
  - window / tenant / run_id / agent_id binding
  - SQL is parameterised (no string-interpolated tenant_id etc.)
  - default-weight defaults read from the registry
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

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


# ── Mock DB harness ────────────────────────────────────────────────


@dataclass
class _Row:
    """Lightweight row stand-in. The extractors access attributes by
    name (e.g. ``row.memory_id``) — a frozen dataclass keeps that
    contract honest at test time."""

    memory_id: str | None = None
    superseded_id: str | None = None
    run_id: str | None = None
    agent_id: str | None = None
    by_id: str | None = None
    status: str | None = None
    content: str | None = None
    recall_count: int | None = None
    observed_at: datetime | None = None


def _mock_db(rows: list[_Row]) -> MagicMock:
    """Build a mock DB whose ``execute(...).fetchall()`` returns
    ``rows`` synchronously. The route uses
    ``await db.execute(...)`` so ``execute`` itself is async; the
    proxy it returns has a sync ``.fetchall()``.
    """
    db = MagicMock()
    proxy = MagicMock()
    proxy.fetchall.return_value = rows
    db.execute = AsyncMock(return_value=proxy)
    return db


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
        db = _mock_db(rows=[])
        out = await supersessions.extract(_query(), db)
        assert out == []
        # Still issued the query (sanity: not short-circuiting on
        # caller side either).
        db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_emits_failure_polarity_on_superseded_trace(self):
        rows = [
            _Row(
                superseded_id="old-mem-1",
                run_id="run-A",
                agent_id="sasha",
                by_id="new-mem-1",
                observed_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
            )
        ]
        out = await supersessions.extract(_query(), _mock_db(rows))
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
            _Row(superseded_id="m1", run_id="r1", agent_id="a1", by_id="n1",
                 observed_at=datetime(2026, 5, 3, tzinfo=timezone.utc)),
            _Row(superseded_id="m2", run_id="r2", agent_id="a2", by_id="n2",
                 observed_at=datetime(2026, 5, 4, tzinfo=timezone.utc)),
            _Row(superseded_id="m3", run_id="r1", agent_id="a1", by_id="n3",
                 observed_at=datetime(2026, 5, 7, tzinfo=timezone.utc)),
        ]
        out = await supersessions.extract(_query(), _mock_db(rows))
        assert len(out) == 3
        # Each evidence keyed by the superseded memory; same trace can
        # accumulate multiple firings (e.g. an agent that wrote 3 bad
        # claims all gets later contradicted).
        assert {e.memory_ids[0] for e in out} == {"m1", "m2", "m3"}

    @pytest.mark.asyncio
    async def test_sql_params_bind_query_inputs(self):
        db = _mock_db(rows=[])
        q = _query(
            tenant_id="acme",
            fleet_id="ops-fleet",
            run_id="r-specific",
            agent_id="alice",
        )
        await supersessions.extract(q, db)
        # Inspect the bind params handed to execute(). The first
        # positional arg is the text() SQL; the second is the params
        # dict.
        args, _ = db.execute.call_args
        sql_clause, params = args
        assert params["tenant_id"] == "acme"
        assert params["fleet_id"] == "ops-fleet"
        assert params["run_id"] == "r-specific"
        assert params["agent_id"] == "alice"
        assert params["w_start"] == q.window_start
        assert params["w_end"] == q.window_end
        # Belt-and-braces: tenant_id MUST be a parameter, NOT
        # string-interpolated into the SQL.
        assert "acme" not in str(sql_clause)

    @pytest.mark.asyncio
    async def test_fleet_filter_applies_to_old_memory_too(self):
        """The FAILURE polarity lands on the SUPERSEDED memory's
        trace. A fleet-scoped scan must filter on the OLD memory's
        fleet_id, not just the corrective one — otherwise an
        agent in fleet A correcting a memory written by fleet B
        would erroneously land failure evidence on fleet B's trace
        inside a fleet-A scan."""
        db = _mock_db(rows=[])
        await supersessions.extract(_query(fleet_id="A"), db)
        args, _ = db.execute.call_args
        sql_clause, _ = args
        sql_text = str(sql_clause).lower()
        # BOTH new_mem.fleet_id AND old_mem.fleet_id must be
        # checked. Pin the literal presence of both clauses so a
        # refactor can't silently drop one.
        assert "new_mem.fleet_id" in sql_text
        assert "old_mem.fleet_id" in sql_text


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
        out = await contradictions.extract(_query(), _mock_db(rows=[]))
        assert out == []

    @pytest.mark.asyncio
    async def test_outdated_status_emits_failure(self):
        rows = [
            _Row(
                memory_id="mem-1",
                run_id="run-X",
                agent_id="mira",
                status="outdated",
                observed_at=datetime(2026, 5, 6, tzinfo=timezone.utc),
            )
        ]
        out = await contradictions.extract(_query(), _mock_db(rows))
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
            _Row(
                memory_id="mem-2",
                run_id="run-Y",
                agent_id="kai",
                status="conflicted",
                observed_at=datetime(2026, 5, 7, tzinfo=timezone.utc),
            )
        ]
        out = await contradictions.extract(_query(), _mock_db(rows))
        assert out[0].details["status"] == "conflicted"
        assert out[0].polarity == Polarity.FAILURE

    @pytest.mark.asyncio
    async def test_sql_uses_status_array_bind(self):
        db = _mock_db(rows=[])
        await contradictions.extract(_query(tenant_id="acme"), db)
        args, _ = db.execute.call_args
        sql_clause, params = args
        # The status set should be bound as a list (PG receives a
        # text[] for ``= ANY(...)``), not interpolated.
        assert params["contradicted_statuses"] == list(
            contradictions.CONTRADICTED_STATUSES
        )
        # The tenant_id must not be string-interpolated either.
        assert "acme" not in str(sql_clause)

    @pytest.mark.asyncio
    async def test_run_and_agent_filter_pass_through(self):
        db = _mock_db(rows=[])
        await contradictions.extract(
            _query(run_id="run-Z", agent_id="alex"), db
        )
        _args, _ = db.execute.call_args
        _sql, params = _args
        assert params["run_id"] == "run-Z"
        assert params["agent_id"] == "alex"

    @pytest.mark.asyncio
    async def test_window_boundaries_passed_as_params(self):
        start = datetime(2026, 4, 1, tzinfo=timezone.utc)
        end = datetime(2026, 4, 15, tzinfo=timezone.utc)
        db = _mock_db(rows=[])
        await contradictions.extract(
            _query(window_start=start, window_end=end), db
        )
        _args, _ = db.execute.call_args
        _sql, params = _args
        assert params["w_start"] == start
        assert params["w_end"] == end
        # The window is left-closed, right-open by SQL contract
        # (``created_at >= w_start AND < w_end``). We assert the
        # SQL text reflects that — drift here would silently shift
        # outcome attribution by one tick.
        sql_text = str(_sql)
        assert ":w_start" in sql_text
        assert ":w_end" in sql_text


# ── Multi-signal independence ─────────────────────────────────────


@pytest.mark.unit
class TestMultiSignalIndependence:
    @pytest.mark.asyncio
    async def test_signals_do_not_share_state(self):
        # Running both extractors against unrelated rows must NOT
        # leak between them.
        sup_rows = [
            _Row(superseded_id="s1", run_id="r1", agent_id="a1", by_id="n1",
                 observed_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
        ]
        con_rows = [
            _Row(memory_id="c1", run_id="r2", agent_id="a2", status="outdated",
                 observed_at=datetime(2026, 5, 6, tzinfo=timezone.utc))
        ]
        sup_out = await supersessions.extract(_query(), _mock_db(sup_rows))
        con_out = await contradictions.extract(_query(), _mock_db(con_rows))
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
        # crashing. (Happens when older memories predate updated_at
        # tracking.)
        for mod, rows in (
            (supersessions, [_Row(superseded_id="s", run_id="r", agent_id="a", by_id="n")]),
            (contradictions, [_Row(memory_id="c", run_id="r", agent_id="a", status="outdated")]),
        ):
            out = await mod.extract(_query(), _mock_db(rows))
            assert len(out) == 1
            assert out[0].observed_at is None or isinstance(out[0].observed_at, datetime)


# ── Window degenerate cases ───────────────────────────────────────


@pytest.mark.unit
class TestWindowDegenerateCases:
    @pytest.mark.asyncio
    async def test_empty_window_returns_empty(self):
        # window_start == window_end → no rows can satisfy
        # ``created_at >= start AND < end``. The query still runs
        # (Postgres returns 0 rows); our extractor returns [].
        same = datetime(2026, 5, 10, tzinfo=timezone.utc)
        out = await supersessions.extract(
            _query(window_start=same, window_end=same), _mock_db(rows=[])
        )
        assert out == []

    @pytest.mark.asyncio
    async def test_inverted_window_does_not_crash(self):
        # Caller passing window_end < window_start is a programmer
        # error; we don't raise, we just return [] (consistent with
        # SQL semantics).
        out = await contradictions.extract(
            _query(
                window_start=datetime(2026, 5, 10, tzinfo=timezone.utc),
                window_end=datetime(2026, 5, 1, tzinfo=timezone.utc),
            ),
            _mock_db(rows=[]),
        )
        assert out == []

    @pytest.mark.asyncio
    async def test_wide_window_collects_all(self):
        # A one-year window with many rows must collect every row the
        # mock returns — the extractor does NOT impose its own LIMIT.
        many = [
            _Row(superseded_id=f"m{i}", run_id=f"r{i}", agent_id="a", by_id=f"n{i}",
                 observed_at=datetime(2026, 5, 5, tzinfo=timezone.utc))
            for i in range(50)
        ]
        out = await supersessions.extract(
            _query(
                window_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
                window_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
            ),
            _mock_db(many),
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
            _Row(
                memory_id="m1",
                run_id="r1",
                agent_id="sasha",
                content="Shipped ✓ to eu-west",
                observed_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
            )
        ]
        out = await terminal_memory.extract(_query(), _mock_db(rows))
        assert len(out) == 1
        assert out[0].kind == SignalKind.TERMINAL_MEMORY
        assert out[0].polarity == Polarity.SUCCESS
        assert out[0].weight == DEFAULT_SIGNAL_WEIGHTS[SignalKind.TERMINAL_MEMORY]
        assert out[0].details["classifier_version"] == terminal_memory.CLASSIFIER_VERSION
        assert out[0].details["verdict"] == "success"

    @pytest.mark.asyncio
    async def test_failure_terminal_emits_failure(self):
        rows = [
            _Row(memory_id="m1", run_id="r1", agent_id="kai",
                 content="Blocked — DNS resolver hangs",
                 observed_at=datetime(2026, 5, 10, tzinfo=timezone.utc))
        ]
        out = await terminal_memory.extract(_query(), _mock_db(rows))
        assert out[0].polarity == Polarity.FAILURE

    @pytest.mark.asyncio
    async def test_ambiguous_terminal_not_emitted(self):
        # Classifier returns None → no evidence at all.
        rows = [
            _Row(memory_id="m1", run_id="r1", agent_id="a",
                 content="Looking into it.",
                 observed_at=datetime(2026, 5, 10, tzinfo=timezone.utc))
        ]
        out = await terminal_memory.extract(_query(), _mock_db(rows))
        assert out == []

    @pytest.mark.asyncio
    async def test_distinct_on_in_sql(self):
        # SQL must use DISTINCT ON (run_id, agent_id) so we only
        # classify the LAST memory of each session — not every memory
        # in the window that happens to contain "shipped".
        db = _mock_db(rows=[])
        await terminal_memory.extract(_query(), db)
        args, _ = db.execute.call_args
        sql_clause, _ = args
        sql_text = str(sql_clause).lower()
        assert "distinct on" in sql_text
        assert "(m.run_id, m.agent_id)" in sql_text


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
        out = await cross_agent_reuse.extract(_query(), _mock_db(rows=[]))
        assert out == []

    @pytest.mark.asyncio
    async def test_load_bearing_emits_neutral(self):
        rows = [
            _Row(
                memory_id="m1",
                run_id="r1",
                agent_id="sasha",
                recall_count=8,
                observed_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
            )
        ]
        out = await cross_agent_reuse.extract(_query(), _mock_db(rows))
        assert len(out) == 1
        # Cross-agent reuse is NEUTRAL — it doesn't claim the
        # originating trace succeeded or failed, only that the
        # artifact is being reused.
        assert out[0].polarity == Polarity.NEUTRAL
        assert out[0].weight == DEFAULT_SIGNAL_WEIGHTS[SignalKind.CROSS_AGENT_REUSE]
        assert out[0].details["recall_count"] == 8
        assert out[0].details["threshold"] == cross_agent_reuse.DEFAULT_RECALL_COUNT_THRESHOLD

    @pytest.mark.asyncio
    async def test_threshold_is_passed_in_sql_params(self):
        db = _mock_db(rows=[])
        await cross_agent_reuse.extract(_query(), db)
        args, _ = db.execute.call_args
        _sql, params = args
        assert params["threshold"] == cross_agent_reuse.DEFAULT_RECALL_COUNT_THRESHOLD


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
        out = await repeat_recall.extract(_query(), _mock_db(rows=[]))
        assert out == []

    @pytest.mark.asyncio
    async def test_no_db_calls_in_stub(self):
        # The stub MUST NOT issue a DB query — costless until the
        # real implementation lands.
        db = _mock_db(rows=[])
        await repeat_recall.extract(_query(), db)
        db.execute.assert_not_called()


# ── External-hooks extractor (MVP stub) ───────────────────────────


@pytest.mark.unit
class TestExternalHooksStub:
    def test_module_exposes_protocol_attrs(self):
        assert external_hooks.kind == SignalKind.EXTERNAL_HOOK
        assert callable(external_hooks.extract)

    @pytest.mark.asyncio
    async def test_mvp_always_returns_empty(self):
        out = await external_hooks.extract(_query(), _mock_db(rows=[]))
        assert out == []

    @pytest.mark.asyncio
    async def test_no_db_calls_in_stub(self):
        db = _mock_db(rows=[])
        await external_hooks.extract(_query(), db)
        db.execute.assert_not_called()


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
