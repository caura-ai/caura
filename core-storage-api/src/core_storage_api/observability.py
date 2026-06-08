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
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger("memclaw.observability")

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
    """
    if "slow" in fields:
        raise ValueError("log_request: 'slow' is reserved — do not pass it as a kwarg")
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
