"""Tests for CAURA-602 request-timing observability.

All tests are ``async def`` even though most don't await — if we mix sync
tests into this file the pytest-asyncio session-scoped event loop never
gets initialised, and every async test that runs *after* us crashes with
``RuntimeError: There is no current event loop``. Keeping the whole file
async is the simplest fix.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import pytest

from core_storage_api.observability import (
    _RESERVED_LOG_FIELDS,
    SLOW_QUERY_THRESHOLD_MS,
    PhaseTimer,
    Timer,
    bind_timer,
    db_measure,
    log_request,
)


async def test_timer_accumulates_across_multiple_blocks() -> None:
    t = Timer()
    with t.measure():
        time.sleep(0.01)
    with t.measure():
        time.sleep(0.01)
    # Two 10ms sleeps with a bit of slack for scheduler jitter
    assert t.total_ms >= 18.0


async def test_timer_total_ms_is_zero_without_measure() -> None:
    assert Timer().total_ms == 0.0


async def test_nested_measure_blocks_both_contribute() -> None:
    """Re-entering measure() must add both elapsed windows — the shared
    Timer instance can be visited through nested ``db_measure`` blocks if
    a service method ever calls another."""
    t = Timer()
    with t.measure():
        time.sleep(0.005)
        with t.measure():
            time.sleep(0.005)
    assert t.total_ms >= 13.0  # 5ms + (5ms + nested 5ms)


async def test_db_measure_is_noop_without_bound_timer() -> None:
    with db_measure():
        time.sleep(0.002)


async def test_db_measure_records_to_bound_timer() -> None:
    with bind_timer() as t:
        with db_measure():
            time.sleep(0.005)
        with db_measure():
            time.sleep(0.005)
    assert t.total_ms >= 8.0


async def test_bind_timer_isolates_per_task() -> None:
    """ContextVar means concurrent tasks see their own timer, not each
    other's — a regression here would mean concurrent requests all land
    in the same accumulator and produce garbage db_ms numbers."""

    async def _task(sleep_ms: int) -> float:
        with bind_timer() as t:
            with db_measure():
                await asyncio.sleep(sleep_ms / 1000)
        return t.total_ms

    results = await asyncio.gather(_task(10), _task(30), _task(20))
    assert results[0] < 25.0
    assert 25.0 <= results[1] < 50.0
    assert 15.0 <= results[2] < 35.0


async def test_log_request_uses_info_for_fast_queries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="caura.observability")
    log_request("scored-search", tenant_id="t1", db_ms=10.0, total_ms=12.0, row_count=3)
    rec = caplog.records[-1]
    assert rec.levelno == logging.INFO
    assert rec.path == "scored-search"
    assert rec.tenant_id == "t1"
    assert rec.db_ms == 10.0
    assert rec.row_count == 3
    assert rec.slow is False


async def test_log_request_upgrades_to_warning_when_slow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="caura.observability")
    log_request(
        "scored-search",
        tenant_id="t1",
        db_ms=SLOW_QUERY_THRESHOLD_MS + 1,
        total_ms=500.0,
    )
    rec = caplog.records[-1]
    assert rec.levelno == logging.WARNING
    assert rec.slow is True


async def test_log_request_rejects_reserved_kwargs() -> None:
    """``slow`` is computed inside log_request; a caller accidentally
    passing it would silently overwrite the computed value and drop the
    WARNING upgrade for slow queries. (``path`` is a positional param,
    so Python's own binding rejects ``path=x`` as a duplicate — no
    observability-side test needed.)"""
    with pytest.raises(ValueError, match="reserved"):
        log_request("scored-search", slow=True, db_ms=10.0)


@pytest.mark.parametrize("reserved", ["module", "message", "process", "args", "name"])
async def test_log_request_rejects_names_logging_owns(reserved: str) -> None:
    """``extra`` cannot overwrite a LogRecord attribute — ``Logger.makeRecord``
    raises ``KeyError: "Attempt to overwrite 'module' in LogRecord"`` instead.

    Unguarded, a field named after one of these turns a request that had
    already SUCCEEDED into a 500: ``log_request`` runs on the router-level
    timing path of hot reads, after the work is done, so the log line is the
    only thing that failed. Rejecting at the call boundary makes it a one-line
    fix instead of an incident.

    Parametrised over five of the 23 names rather than all of them: the point
    is that the guard uses the same derived set ``logging`` enforces, and
    ``test_the_reserved_set_matches_what_logging_enforces`` below is what pins
    that. ``message`` and ``asctime`` are worth naming individually — they are
    not attributes of a fresh record, they are filled in during formatting, so
    a hand-written list would plausibly have missed them."""
    with pytest.raises(ValueError, match="reserved"):
        log_request("scored-search", db_ms=10.0, **{reserved: "x"})


async def test_the_reserved_set_matches_what_logging_enforces() -> None:
    """The guard is only as good as the set it checks against, and that set is
    derived from a sample record precisely so it cannot drift from the one
    ``makeRecord`` enforces. This checks the derivation against the real
    exception rather than against a copy of the list.

    Fails on a Python release that adds a LogRecord attribute, which is the
    moment the guard would otherwise start silently missing one."""
    logger_ = logging.getLogger("caura.observability.reserved-probe")
    for name in _RESERVED_LOG_FIELDS - {"message", "asctime"}:
        with pytest.raises(KeyError):
            logger_.warning("probe", extra={name: "x"})

    # The two that are NOT on a fresh record: filled in by the formatter, so
    # ``makeRecord`` lets them through and they are added to the set by hand.
    for name in ("message", "asctime"):
        assert name in _RESERVED_LOG_FIELDS


async def test_log_request_still_accepts_ordinary_fields() -> None:
    """The guard must not have widened into the fields real callers pass —
    every name used by the three production call sites in routers/memories.py."""
    log_request(
        "scored-search",
        tenant_id="t1",
        top_k=10,
        total_ms=12.0,
        db_ms=10.0,
        row_count=3,
        id_count=2,
        hit=True,
        has_date_range=False,
        has_temporal_window=False,
        error=False,
    )


async def test_log_request_without_db_ms_stays_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """GET hits that miss the cache don't always carry db_ms — the helper
    must not crash or upgrade severity when the field isn't present."""
    caplog.set_level(logging.INFO, logger="caura.observability")
    log_request("memory-get", tenant_id="t1", total_ms=5.0, hit=False)
    rec = caplog.records[-1]
    assert rec.levelno == logging.INFO
    assert rec.slow is False


async def test_end_to_end_memory_get_404_emits_log_line(
    sc, caplog: pytest.LogCaptureFixture
) -> None:
    """End-to-end wiring: GET /memories/{id} miss flows through bind_timer,
    db_measure, and log_request; the log line carries db_ms from a real
    session.get() call."""
    from uuid import uuid4

    caplog.set_level(logging.INFO, logger="caura.observability")
    result = await sc.get_memory(uuid4(), "t1")
    assert result is None

    obs_records = [r for r in caplog.records if r.name == "caura.observability"]
    assert obs_records, "expected at least one caura.observability log record"
    rec = obs_records[-1]
    assert rec.path == "memory-get"
    assert rec.hit is False
    assert rec.total_ms > 0
    assert rec.db_ms > 0
    assert rec.db_ms <= rec.total_ms


# ---------------------------------------------------------------------------
# PhaseTimer (caura#1616)
#
# The operation these exist for is one that never returns: Cloud Run severs
# the request at 120s and the caller sees a 504 naming the endpoint. So the
# cases that matter are the failure ones, and the specific thing under test
# is that an UNFINISHED phase is reported as unfinished.
# ---------------------------------------------------------------------------


async def test_phase_timer_names_the_phase_in_flight_when_cancelled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The production case. A severed request reaches the block as
    ``asyncio.CancelledError``, which is a ``BaseException`` — it slips past
    an ``except Exception`` written for this job, so the report has to come
    from ``__aexit__``."""
    caplog.set_level(logging.WARNING, logger="caura.observability")

    with pytest.raises(asyncio.CancelledError):
        async with PhaseTimer("discover", tenant_id="t1") as phases:
            with phases.phase("candidates"):
                pass
            with phases.phase("lateral"):
                raise asyncio.CancelledError

    rec = caplog.records[-1]
    assert rec.levelno == logging.WARNING
    assert rec.phase == "lateral"
    assert rec.operation == "discover"
    assert rec.tenant_id == "t1"
    assert rec.total_s >= 0.0


async def test_phase_timer_does_not_file_an_unfinished_phase_as_completed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one that guards the design.

    ``phase()`` records a phase only on the line AFTER its ``yield``, so a
    phase that raised never lands in the breakdown. Rewriting that as a
    ``finally`` — the reflex change — would file ``lateral`` as completed,
    clear the in-flight name, and leave the warning with nothing to report
    but ``(between phases)``. The whole point is lost silently, so it is
    asserted directly."""
    caplog.set_level(logging.WARNING, logger="caura.observability")

    with pytest.raises(RuntimeError):
        async with PhaseTimer("discover") as phases:
            with phases.phase("candidates"):
                pass
            with phases.phase("lateral"):
                raise RuntimeError("storage did not answer")

    message = caplog.records[-1].getMessage()
    assert "candidates=" in message, "a phase that finished belongs in the breakdown"
    assert "phase=lateral" in message, "the unfinished phase must be named as in flight"
    assert "lateral=" in message and "(unfinished)" in message, (
        "and reported as unfinished, not as a completed timing"
    )


async def test_phase_timer_says_between_phases_when_nothing_was_running(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failing outside every phase is its own answer, not a missing one: for
    a caller wrapping a database session it means connection acquisition or
    COMMIT. Naming the last phase that happened to finish would be a lie."""
    caplog.set_level(logging.WARNING, logger="caura.observability")

    with pytest.raises(RuntimeError):
        async with PhaseTimer("discover") as phases:
            with phases.phase("candidates"):
                pass
            raise RuntimeError("commit hung")

    assert caplog.records[-1].phase == "(between phases)"


async def test_phase_timer_logs_nothing_when_the_block_succeeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every ingest takes this path. Instrumentation that adds a line per
    call per phase would be paid for on every healthy request forever; the
    caller folds ``breakdown()`` into a line it already emits instead."""
    caplog.set_level(logging.INFO, logger="caura.observability")

    async with PhaseTimer("discover") as phases:
        with phases.phase("candidates"):
            pass

    assert [r for r in caplog.records if r.name == "caura.observability"] == []


async def test_phase_timer_breakdown_reports_phases_and_remainder() -> None:
    async with PhaseTimer("discover") as phases:
        with phases.phase("candidates"):
            time.sleep(0.01)
        with phases.phase("lateral"):
            pass
        breakdown = phases.breakdown()

    assert "candidates=0.0s" in breakdown
    assert "lateral=0.0s" in breakdown
    # Time inside the block but outside every phase, which is where a slow
    # COMMIT or a starved connection pool would show up.
    assert "other=" in breakdown


async def test_phase_timer_does_not_swallow_the_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reporting is a side effect. The caller still has to fail."""
    caplog.set_level(logging.WARNING, logger="caura.observability")

    with pytest.raises(RuntimeError, match="storage did not answer"):
        async with PhaseTimer("discover") as phases:
            with phases.phase("lateral"):
                raise RuntimeError("storage did not answer")


async def test_phase_timer_bills_an_unfinished_phase_to_itself_not_to_other(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reason the review caught this one: for caura#1616 the unfinished
    phase IS the 120 seconds. Folding it into ``other`` produces
    ``other=119.5s`` next to ``phase=lateral`` and sends whoever is on call
    to look at connection overhead — the breakdown contradicting the very
    field beside it."""
    caplog.set_level(logging.WARNING, logger="caura.observability")

    with pytest.raises(RuntimeError):
        async with PhaseTimer("discover") as phases:
            with phases.phase("lateral"):
                time.sleep(0.2)
                raise RuntimeError("storage did not answer")

    message = caplog.records[-1].getMessage()
    lateral = float(re.search(r"lateral=([\d.]+)s\(unfinished\)", message).group(1))
    other = float(re.search(r"other=([\d.]+)s", message).group(1))
    assert lateral >= 0.15, f"the unfinished phase owns its own time: {message}"
    assert other <= 0.1, f"and ``other`` is the residue, not the query: {message}"


async def test_phase_timer_other_covers_teardown_but_stops_at_the_block() -> None:
    """``other`` has to include what the inner context managers do on the way
    out — for the caller this class was built for, that is COMMIT, and a
    summary logged from inside the block would miss it entirely. It must NOT
    keep growing afterwards, or the caller's own logging inflates the figure
    it is about to log."""
    async with PhaseTimer("discover") as phases:
        with phases.phase("candidates"):
            pass
        time.sleep(0.1)  # stands in for COMMIT, inside the block, in no phase
    time.sleep(0.1)  # the caller's own work before it emits its summary

    other = float(re.search(r"other=([\d.]+)s", phases.breakdown()).group(1))
    assert 0.05 <= other <= 0.18, f"expected ~0.1s of teardown only, got {other}s"


@pytest.mark.parametrize("bad", ["module", "message", "process", "phase", "total_s"])
async def test_phase_timer_rejects_a_field_that_would_break_the_log_record(
    bad: str,
) -> None:
    """``logging`` raises KeyError rather than overwriting a LogRecord
    attribute, and the only ``extra`` this class ever passes is the one in
    ``__aexit__`` — mid-incident, where a KeyError would replace the exception
    the caller needs with one about logging. So the check runs at construction,
    where it fires on a healthy request instead. Same idiom as
    ``log_request``'s reserved ``slow`` kwarg."""
    with pytest.raises(ValueError, match="reserved"):
        PhaseTimer("discover", **{bad: "x"})


async def test_phase_timer_own_log_fields_matches_what_it_actually_sets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Drift guard, in both directions. ``_OWN_LOG_FIELDS`` is what callers are
    forbidden from passing; if ``__aexit__`` grows a fourth field and the tuple
    is not updated, that field becomes silently overwritable by a caller — the
    collision the guard above exists to prevent, reintroduced quietly."""
    caplog.set_level(logging.WARNING, logger="caura.observability")

    with pytest.raises(RuntimeError):
        async with PhaseTimer("discover", tenant_id="t1") as phases:
            with phases.phase("lateral"):
                raise RuntimeError("boom")

    standard = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
        "message",
        "asctime",
        "taskName",
    }
    carried = set(vars(caplog.records[-1])) - standard
    assert carried == set(PhaseTimer._OWN_LOG_FIELDS) | {"tenant_id"}


async def test_phase_timer_refuses_a_nested_phase() -> None:
    """Nesting does not just mis-total — it misnames. The inner phase clears
    ``_current`` on its way out, so an interruption in the OUTER phase would
    be reported as ``(between phases)``: a confident wrong answer, produced
    during the incident. Loud on the first healthy call instead."""
    async with PhaseTimer("discover") as phases:
        with phases.phase("candidates"):
            with pytest.raises(RuntimeError, match="sequential, not nested"):
                with phases.phase("lateral"):
                    pass


async def test_phase_timer_allows_sequential_phases_after_one_completes() -> None:
    """The guard must not misfire on the ordinary case it sits next to."""
    async with PhaseTimer("discover") as phases:
        with phases.phase("candidates"):
            pass
        with phases.phase("lateral"):
            pass
        breakdown = phases.breakdown()

    assert "candidates=" in breakdown and "lateral=" in breakdown
