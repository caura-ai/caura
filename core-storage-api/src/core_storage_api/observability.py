"""Structured timing logs for the hot read paths (CAURA-602).

Emits one structured log line per wrapped request with ms-level breakdowns
so we can answer latency questions in Cloud Logging without a new loadtest.

The plumbing is intentionally small:
- ``Timer`` accumulates ms across one or more ``measure()`` blocks.
- ``bind_timer()`` is scoped at the router: it creates a Timer and stashes
  it in a ContextVar so the service layer can opt into contributing spans
  without receiving a timer argument.
- ``db_measure()`` is the service-layer entry point: it's a no-op when no
  timer is bound (so tests and other callers aren't forced to pay for
  the plumbing).
- ``PhaseTimer`` answers the question the three above cannot: for an
  operation that never returns, which of its steps was still running.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from types import TracebackType
from typing import Any, Literal

logger = logging.getLogger("caura.observability")

# db_ms above this gets upgraded to WARNING so slow queries surface in
# Cloud Run's error-filtered log views (where on-call lives) without us
# hand-crafting a logging query each time. Override via env var when the
# real p95 moves after a capacity change (no redeploy-to-tune needed).
SLOW_QUERY_THRESHOLD_MS: float = float(os.getenv("SLOW_QUERY_THRESHOLD_MS", "250"))

# Reused across every un-instrumented DB call (every request that isn't
# bound to a Timer) — hoist out of the hot path so we don't allocate.
_NULLCONTEXT = nullcontext()


class Timer:
    """Accumulating ms timer. Safe to re-enter ``measure()`` (each block
    contributes its own elapsed time, no shared start-state)."""

    __slots__ = ("total_ms",)

    def __init__(self) -> None:
        self.total_ms = 0.0

    @contextmanager
    def measure(self) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.total_ms += (time.perf_counter() - start) * 1000


# Keys ``logging`` refuses to let ``extra`` set: it raises KeyError rather than
# overwriting them. Derived from a sample record instead of hand-listed so the
# set cannot drift from the one ``Logger.makeRecord`` actually enforces (it
# checks ``rv.__dict__`` plus these two, which are filled in later).
_SAMPLE_RECORD = logging.LogRecord("", 0, "", 0, "", (), None)
_RESERVED_LOG_FIELDS = frozenset(vars(_SAMPLE_RECORD)) | {"message", "asctime"}


class PhaseTimer:
    """Name the step a multi-step operation was in when it ended badly.

    ``Timer`` above answers "how much of this request was the database".
    This answers a different question, and one that a request which never
    returns is otherwise unable to answer at all: *which step was still
    running when we were killed*.

    That difference drives the whole shape. A step that hangs never logs
    its own completion, so a breakdown assembled at the end of the
    operation is precisely the breakdown a timed-out request never
    reaches. Reporting therefore happens on the way OUT, from
    ``__aexit__`` — which runs whether the block returned, raised, or was
    cancelled. Cancellation is the case that matters here and the case a
    hand-written guard usually misses: ``asyncio.CancelledError`` is a
    ``BaseException``, so it slips straight past an ``except Exception``
    written for exactly this job.

    Nothing is logged when the block succeeds. Callers that want the
    breakdown on the happy path fold ``breakdown()`` into a line they
    already emit, so instrumenting an operation costs no extra log volume
    until something actually goes wrong.
    """

    __slots__ = ("_completed", "_current", "_end", "_fields", "_operation", "_start")

    #: Set by ``__aexit__`` itself; a caller-supplied field of the same name
    #: would be silently dropped rather than reported.
    _OWN_LOG_FIELDS = ("operation", "phase", "total_s")

    def __init__(self, operation: str, **fields: Any) -> None:
        # Validated HERE, not at the point of logging. A field colliding with a
        # LogRecord attribute makes ``logger.warning`` raise KeyError — and the
        # only call that matters happens inside ``__aexit__`` while an incident
        # is already unwinding, where it would replace the exception the caller
        # needs to see with one about logging. Failing at construction moves
        # that to a healthy request, loudly, where it is a one-line fix.
        reserved = _RESERVED_LOG_FIELDS | set(self._OWN_LOG_FIELDS)
        clashes = sorted(set(fields) & reserved)
        if clashes:
            raise ValueError(
                f"PhaseTimer: field name(s) {', '.join(clashes)} are reserved — "
                "rename them; they cannot be carried on the log record"
            )
        self._operation = operation
        self._fields = fields
        self._completed: list[tuple[str, float]] = []
        # Name AND start time: an unfinished phase still has a duration, and
        # it is the most interesting duration in the whole breakdown.
        self._current: tuple[str, float] | None = None
        self._start = 0.0
        self._end: float | None = None

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Time one step of the operation. Steps are sequential, not nested.

        Enforced, not merely documented: a nested ``phase()`` would overwrite
        the outer step's name and then clear it on the way out, so an
        interruption in the outer step would be reported as happening between
        phases — a wrong answer, produced during exactly the incident this
        class exists to explain. Raising turns that into a failure on the
        first healthy request instead, the same trade the reserved-field check
        in ``__init__`` makes.
        """
        if self._current is not None:
            raise RuntimeError(
                f"PhaseTimer.phase({name!r}) called while "
                f"{self._current[0]!r} is still in flight — "
                "phases are sequential, not nested"
            )
        started = time.perf_counter()
        self._current = (name, started)
        yield
        # Nothing below runs when the block raises or is cancelled: the
        # exception is thrown in at the ``yield`` and propagates from
        # there. That is deliberate and load-bearing, not an oversight —
        # a phase that did not finish has to stay "in flight" so
        # ``__aexit__`` can name it. A ``finally`` here would file the
        # phase as completed and clear ``_current``, destroying the exact
        # signal this class exists to emit.
        self._completed.append((name, time.perf_counter() - started))
        self._current = None

    def breakdown(self) -> str:
        """Every phase that ran, plus the time that was inside no phase at all.

        A phase still running is reported under its own name and marked
        ``(unfinished)``, NOT folded into ``other``. Folding it there is
        actively misleading in the only situation that matters: a query
        killed at 120s would read ``other=119.5s`` and send whoever is on
        call looking at connection overhead, when the breakdown's own
        ``phase=`` field is naming the query. The marker goes after the
        ``=`` so that grepping ``lateral=`` still finds both forms.

        ``other`` is then genuinely the residue — for a caller that wraps
        a database session, connection acquisition plus, if the breakdown
        is taken after the block closes, COMMIT. Either can be the slow
        thing while every named phase looks healthy.
        """
        # Frozen at ``__aexit__`` so a breakdown taken after the block does
        # not keep inflating ``other`` with the caller's own logging time.
        now = self._end if self._end is not None else time.perf_counter()
        parts = [f"{name}={seconds:.1f}s" for name, seconds in self._completed]
        accounted = sum(seconds for _, seconds in self._completed)
        if self._current is not None:
            name, started = self._current
            running = now - started
            accounted += running
            parts.append(f"{name}={running:.1f}s(unfinished)")
        parts.append(f"other={now - self._start - accounted:.1f}s")
        return " ".join(parts)

    async def __aenter__(self) -> PhaseTimer:
        self._start = time.perf_counter()
        return self

    # Returns ``Literal[False]``, not ``bool``: this class reports and gets out
    # of the way, and the annotation is what says so to anyone reading the
    # signature. mypy enforces the same promise from the other side — as
    # ``bool`` it makes every ``async with`` a possible swallow point, which
    # turned the one wrapping this into "Missing return statement".
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        self._end = time.perf_counter()
        if exc_type is None:
            return False
        total = self._end - self._start
        # ``_current`` is None when the operation died between phases —
        # for a session-wrapping caller, during acquisition or COMMIT.
        # Naming that explicitly beats naming the phase that last
        # finished, which is what a "last known phase" field would report
        # and would be a lie.
        in_flight = self._current[0] if self._current is not None else "(between phases)"
        logger.warning(
            "%s interrupted after %.1fs during phase=%s [%s]",
            self._operation,
            total,
            in_flight,
            self.breakdown(),
            extra={
                **self._fields,
                "operation": self._operation,
                "phase": in_flight,
                "total_s": round(total, 3),
            },
        )
        return False


_current_timer: ContextVar[Timer | None] = ContextVar("_current_timer", default=None)


@contextmanager
def bind_timer() -> Iterator[Timer]:
    """Router-scope: create a Timer and make it visible to ``db_measure``."""
    timer = Timer()
    token = _current_timer.set(timer)
    try:
        yield timer
    finally:
        _current_timer.reset(token)


def db_measure() -> Any:
    """Service-scope: a context manager that adds elapsed time to the
    currently-bound Timer, or a no-op if no router has bound one."""
    timer = _current_timer.get()
    if timer is None:
        return _NULLCONTEXT
    return timer.measure()


def log_request(path: str, **fields: Any) -> None:
    """Emit a single INFO (or WARNING, if slow) line for a completed request.

    ``path`` becomes the structured ``path`` field so Cloud Logging can
    filter by endpoint. Any additional kwargs are passed through as
    structured fields (``tenant_id``, ``total_ms``, ``db_ms``, ...).

    ``slow`` is reserved — it's computed from ``db_ms`` to drive the
    INFO/WARNING split. (``path`` is a positional param, so Python's own
    argument binding already rejects a duplicate.)

    So is every name ``logging`` puts on a ``LogRecord``. ``extra`` cannot
    overwrite one: ``Logger.makeRecord`` raises ``KeyError: "Attempt to
    overwrite 'module' in LogRecord"`` instead. This runs on the router-level
    timing path of hot reads, so an unguarded ``module=``/``message=``/
    ``process=`` field would turn a request that had already SUCCEEDED into a
    500 — the log line being the only thing that failed. Rejected here, where
    it is a one-line fix at the call site, rather than at the ``logger.log``
    below, where it is an incident.

    Same set and same reasoning as :class:`PhaseTimer`; see
    ``_RESERVED_LOG_FIELDS`` for why it is derived rather than listed.
    """
    if "slow" in fields:
        raise ValueError("log_request: 'slow' is reserved — do not pass it as a kwarg")
    # Checked separately from ``slow`` above, whose message says WHY it is
    # reserved (it is computed here, and a caller passing it would drop the
    # WARNING upgrade). Collapsing the two would trade that for a generic one.
    clashes = sorted(set(fields) & _RESERVED_LOG_FIELDS)
    if clashes:
        raise ValueError(
            f"log_request: field name(s) {', '.join(clashes)} are reserved by "
            "logging — rename them; they cannot be carried on the log record"
        )
    db_ms = fields.get("db_ms")
    slow = isinstance(db_ms, (int, float)) and db_ms > SLOW_QUERY_THRESHOLD_MS
    fields["path"] = path
    fields["slow"] = slow
    logger.log(
        logging.WARNING if slow else logging.INFO,
        "request_timing path=%s",
        path,
        extra=fields,
    )
