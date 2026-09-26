"""Request-scoped phase attribution for the request budget (ax-0917-h-01/h-02).

``RequestTimeoutMiddleware`` cancels a handler at
``request_timeout_seconds`` and answers ``REQUEST_BUDGET_EXCEEDED`` with the
budget, the elapsed time and the path. That says the deadline passed; it does
not say WHICH layer consumed it. Two entirely different incidents — a stalled
embedding provider and a stalled storage read — produced byte-identical
evidence, so "the next occurrence will say which layer ate the budget" was not
true of the shipped instrumentation.

The same principle is already written down one layer lower, beside
``EMBEDDING_GATE_TIMEOUT_SECONDS``: *"a cancellation carries no attribution —
it says the deadline passed, not which layer ate it."* That module solved it
for itself by failing under the caller's budget. A blanket middleware has no
equivalent move: it is the outermost deadline, so nothing below it can fail
first on its behalf. It has to be TOLD what was running.

So each bounded hop announces itself with :func:`phase`, and the recorder
keeps the stack. On timeout the middleware reports the phases that were still
open — innermost first, which is the layer — plus the ones that had already
completed and how long each took, which is what turns "slow" into "slow HERE".

Why a mutable object behind a ContextVar rather than the ContextVar carrying
the data: ``BaseHTTPMiddleware`` (SlowAPI) runs the downstream app in a
separate anyio task, and so does every ``asyncio.ensure_future`` on the search
path. A child task inherits a COPY of the context, so a ``set()`` down there
is invisible up here — but the copy binds the same object, so mutations to it
are not. That is why the middleware creates the recorder before the split and
never re-sets the var.

Cost: one ``perf_counter`` and one list append per phase, on a path that
already times every pipeline step. ``phase()`` is a no-op when no recorder is
bound (background tasks, anything outside a request), so instrumented code is
safe to call from anywhere.

The middleware is not the only thing that arms a recorder. Three routes opt
out of it and enforce their own ``asyncio.wait_for``, and the MCP mount is
skipped entirely — all four were left with the exact failure this module
exists to fix. They arm via :func:`own_deadline`, which documents why arming
below the ``BaseHTTPMiddleware`` split is sound for a caller that catches its
own deadline.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

# Caps on what reaches the 504 body and the log line. A search runs 11 steps
# plus its storage hops; a write pipeline is comparable. 32 completed phases
# covers both with headroom, and the count of what was dropped is reported so
# a truncated list never reads as a complete one.
_MAX_COMPLETED = 32
_MAX_OPEN = 16


class RequestPhases:
    """The phase stack for ONE request. Mutated from any task in its context."""

    __slots__ = (
        "_cancelled",
        "_completed",
        "_deadline",
        "_dropped",
        "_failed",
        "_next_id",
        "_open",
        "_started_at",
    )

    def __init__(self, budget_seconds: float | None = None) -> None:
        self._started_at = time.monotonic()
        self._deadline = None if budget_seconds is None else self._started_at + budget_seconds
        # id -> (name, entered_at). A dict rather than a list because phases
        # nest AND overlap: the search step races the embedding and the entity
        # boost in separate tasks, so exits are not LIFO.
        self._open: dict[int, tuple[str, float]] = {}
        self._completed: list[tuple[str, float]] = []
        # Phases that unwound on an exception AT OR PAST the deadline — i.e.
        # the cancellation. This list IS the stack that was in flight when the
        # budget blew, innermost first (the deepest ``finally`` runs first).
        self._cancelled: list[tuple[str, float]] = []
        # Phases that unwound on an exception BEFORE the deadline, which is a
        # different event and must not be mistaken for it. A hop whose failure
        # the caller SWALLOWS and continues past is the case that matters: bulk
        # embed does exactly that on a deferred deployment (it logs and falls
        # through to the backfill). Filed as a cancellation it would land at
        # ``_cancelled[0]`` — ahead of the real culprit, since it unwound
        # first — and ``phase`` would name a hop that finished a minute before
        # the deadline. That is a confidently wrong attribution, which is worse
        # than the none this module was written to replace.
        self._failed: list[tuple[str, float]] = []
        self._dropped = 0
        self._next_id = 0

    def enter(self, name: str) -> int:
        token = self._next_id
        self._next_id += 1
        if len(self._open) < _MAX_OPEN:
            self._open[token] = (name, time.monotonic())
        return token

    def exit(self, token: int, *, failed: bool) -> None:
        entry = self._open.pop(token, None)
        if entry is None:
            return
        name, entered_at = entry
        record = (name, round(time.monotonic() - entered_at, 3))
        if not failed:
            bucket = self._completed
        elif self._deadline is None or self.past_deadline():
            # No deadline means the caller has no clock to sort failures
            # against (the MCP transport arms the recorder with none), so every
            # unwind is reported as the in-flight stack — which is exactly what
            # it is for a caller reporting an exception rather than a timeout.
            bucket = self._cancelled
        else:
            bucket = self._failed
        if len(bucket) >= _MAX_COMPLETED:
            self._dropped += 1
            return
        bucket.append(record)

    def snapshot(self) -> dict:
        """What the budget-exceeded response and log line report.

        ``phase`` is the single-field answer to "which layer": the innermost
        thing that was still running. It is read from ``_cancelled`` rather
        than ``_open`` because by the time the middleware catches
        ``TimeoutError`` the cancellation has already unwound every ``with``
        block below it — the stack is in the unwind record, not in ``_open``.
        ``_open`` is still consulted for the case where a phase is held by a
        task the cancellation has not reached yet.
        """
        in_flight = [
            {"phase": name, "seconds": round(time.monotonic() - at, 3)}
            for name, at in sorted(self._open.values(), key=lambda e: e[1], reverse=True)
        ]
        cancelled = [{"phase": n, "seconds": s} for n, s in self._cancelled]
        deepest = None
        if cancelled:
            deepest = cancelled[0]["phase"]
        elif in_flight:
            deepest = in_flight[0]["phase"]
        out: dict = {
            "phase": deepest,
            "phases_cancelled": cancelled,
            "phases_completed": [{"phase": n, "seconds": s} for n, s in self._completed],
        }
        if in_flight:
            out["phases_open"] = in_flight
        if self._failed:
            # Reported, not dropped: a hop that failed early and was swallowed
            # is often WHY the request then ran long (a failed inline embed
            # sends the row down the backfill path). It just is not the layer
            # holding the budget at the deadline, so it stays out of
            # ``phases_cancelled`` and out of ``phase``.
            out["phases_failed"] = [{"phase": n, "seconds": s} for n, s in self._failed]
        if self._dropped:
            out["phases_dropped"] = self._dropped
        return out

    def past_deadline(self) -> bool:
        """Has the request budget already expired?

        Lets a layer INSIDE the timeout middleware tell "cancelled by our own
        deadline" from "crashed". It has to be asked rather than told: the
        middleware only learns the budget blew after every inner ``finally``
        has already run, so by the time it could set a flag, the layers that
        need the answer are gone. The comparison is exact, not approximate —
        ``asyncio.timeout`` fires off ``loop.time()``, which is
        ``time.monotonic()`` for the default event loop, so on the unwinding
        path the clock has provably passed this deadline.
        """
        return self._deadline is not None and time.monotonic() >= self._deadline


_phases: ContextVar[RequestPhases | None] = ContextVar("request_phases", default=None)


def begin(budget_seconds: float | None = None) -> tuple[RequestPhases, Token]:
    """Arm a recorder for this request. Caller MUST :func:`end` the token.

    Resetting matters even though production serves each request in its own
    task: an in-process ASGI transport (every integration test, and the
    storage bridge) calls the app inside the CALLER's context, so a leaked
    binding would let one request's phases land in the next one's report.
    """
    recorder = RequestPhases(budget_seconds)
    return recorder, _phases.set(recorder)


def end(token: Token) -> None:
    _phases.reset(token)


def current() -> RequestPhases | None:
    return _phases.get()


@contextmanager
def own_deadline(budget_seconds: float | None) -> Iterator[RequestPhases]:
    """Arm a recorder for a deadline this caller enforces ITSELF.

    ``RequestTimeoutMiddleware`` is the blanket budget, and three routes opt
    out of it (``/memories/bulk``, ``/admin/org/purge-data``,
    ``/interview/submit``) plus the whole MCP mount. The two that replace it
    with an ``asyncio.wait_for`` of their own were left with the pre-#1707
    failure: a 504 that says the deadline passed and nothing about which layer
    passed it. This is how they get the same answer.

    Arming HERE — inside SlowAPI's ``BaseHTTPMiddleware`` split, which the
    module docstring says is fatal for the middleware — is sound for a reason
    that does not generalise: the caller that arms the recorder is the same
    frame that catches its own ``TimeoutError`` and reads the snapshot. The
    middleware's problem is that it arms ABOVE the split and the phases are
    recorded BELOW it; a route has no split between the two, so the ordinary
    ContextVar rules are enough.

    ``None`` arms a recorder with no deadline, for a caller that has none —
    the MCP transport, which is skipped by the middleware and enforces no
    budget of its own. Nothing will be cancelled by a clock there, so the
    recorder's value is naming the hop an exception came out of.

    Nested use is not the intended shape but is harmless: an opted-out route
    is never under the middleware (that is what opting out means), so the
    only way to nest is a caller inside a caller, and the inner recorder then
    shadows the outer for the duration — the inner deadline is the one that
    will fire, so it is also the one whose phases matter.
    """
    recorder, token = begin(budget_seconds)
    try:
        yield recorder
    finally:
        end(token)


def past_deadline() -> bool:
    """``True`` only inside a budgeted request whose budget has expired."""
    recorder = _phases.get()
    return recorder is not None and recorder.past_deadline()


@contextmanager
def phase(name: str) -> Iterator[None]:
    """Mark ``name`` as the layer running inside this block.

    Synchronous by design: the block it wraps is almost always an ``await``,
    and a sync context manager wraps awaited code perfectly well while costing
    a fraction of ``@asynccontextmanager``'s per-use machinery. Names are
    literals or bounded identifiers (step names, storage route labels, slot
    scopes) — never user input, which would make this a cardinality bomb in
    the log line.
    """
    recorder = _phases.get()
    if recorder is None:
        yield
        return
    token = recorder.enter(name)
    try:
        yield
    except BaseException:
        # ``BaseException``, not ``Exception``: the case this exists for is
        # ``CancelledError``, which is neither.
        recorder.exit(token, failed=True)
        raise
    else:
        recorder.exit(token, failed=False)
