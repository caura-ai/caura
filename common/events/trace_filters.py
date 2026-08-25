"""APM span corrections for Pub/Sub's pull loop.

``PubSubEventBus._pull_loop`` calls ``subscriber.pull(timeout=...)``. When no
message arrives inside that window, gRPC returns ``DEADLINE_EXCEEDED`` and the
pull loop treats it as the non-event it is::

    except gexc.DeadlineExceeded:
        # No messages in the pull window; loop back and try again.
        continue

ddtrace's gRPC integration, however, wraps the call and marks the span failed
on any non-OK status before that ``except`` ever runs
(``contrib/internal/grpc/client_interceptor.py``: ``if response_code !=
grpc.StatusCode.OK`` -> ``span.error = 1``, with no per-status configuration to
opt out of). An idle subscriber therefore manufactures error spans at one per
empty window, forever.

Measured over 24h before this existed: 51,568 such spans from core-api in
staging alone, against 617 in prod — an idle deployment is far noisier than a
busy one, because a pull that returns messages succeeds. In prod this resource
was still the single largest source of error spans (1,223 of ~3,163).

This clears ``span.error`` for that exact case and nothing else. The span is
kept, not dropped: a pull that fails for a real reason (NotFound,
PermissionDenied) still arrives as an error, and the timing of poll waits stays
visible. ``StatusCode.CANCELLED`` — a pull interrupted by shutdown — is also
left alone, since it says something true about the process.

User trace processors run before the writer (``SpanAggregator.on_span_finish``
chains ``user_processors`` ahead of sampling and the writer), so the payload the
Agent receives — and computes APM stats from — already has ``error: 0``. That is
what makes this fix the error-rate metric and any monitor on it, not just what
Error Tracking displays.

The ``error.*`` tags are deliberately left in place. ddtrace 4.x offers no
public way to remove a tag: ``set_tag(k, None)`` stores the *string* ``"None"``,
and the only thing that really removes one is ``Span._remove_attribute``, a
private method on a native-backed span. Since ``span.error`` is what the above
keys on, keeping the tags costs nothing and leaves the timeouts queryable on
purpose (``@error.type:StatusCode.DEADLINE_EXCEEDED``) rather than merely
absent.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ddtrace is an optional dependency — only the `datadog` extra installs it.
# Guarded exactly like common/structlog_config.py so installs without it keep
# importing this module. Top-level (not deferred) so the names are patchable in
# tests.
try:
    from ddtrace.constants import ERROR_TYPE as _ERROR_TYPE
    from ddtrace.trace import TraceFilter as _TraceFilter
    from ddtrace.trace import tracer as _dd_tracer
except ImportError:  # pragma: no cover - exercised only in non-datadog installs
    _ERROR_TYPE = "error.type"
    _TraceFilter = object  # type: ignore[assignment,misc]
    _dd_tracer = None  # type: ignore[assignment]

# The gRPC method the pull loop calls. Matched exactly rather than by prefix so
# a future Pub/Sub call that genuinely fails is never swept up.
PULL_RESOURCE = "/google.pubsub.v1.Subscriber/Pull"

# Substring, not equality: ddtrace records this as the stringified enum
# (``StatusCode.DEADLINE_EXCEEDED``), and that repr is not something to depend
# on staying byte-identical across grpc/ddtrace versions.
_DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"

# Guard so repeated start() calls in one process don't re-register. See
# install() for why re-registering would be actively harmful rather than merely
# redundant.
_installed = False


class PubSubPullDeadlineFilter(_TraceFilter):  # type: ignore[misc,valid-type]
    """Clear ``span.error`` on Pub/Sub pull spans that only timed out."""

    def process_trace(self, trace: list[Any]) -> list[Any]:
        # Runs on every trace this process flushes, so the tests are ordered
        # cheapest-first: an int attribute, then a string compare, and only
        # then the tag lookup (a call into ddtrace's native span).
        for span in trace:
            if not span.error:
                continue
            if span.resource != PULL_RESOURCE:
                continue
            error_type = span.get_tag(_ERROR_TYPE) or ""
            if _DEADLINE_EXCEEDED in error_type:
                span.error = 0
        # Always return the trace — returning None would drop it entirely.
        return trace


def install() -> bool:
    """Register the filter with the global tracer. Idempotent.

    Returns True when the filter is registered, False when it isn't — either
    because ddtrace is absent or because registration failed. Never raises:
    this is a reporting correction, and nothing here is worth failing a
    service's startup over.
    """
    global _installed

    if _dd_tracer is None:
        return False
    if _installed:
        return True

    # configure(trace_processors=...) REPLACES the user-processor list rather
    # than appending to it (Tracer.configure -> _recreate ->
    # SpanAggregator.reset(user_processors=...)), so a bare call here would
    # silently evict any processor registered before us — no error, no warning,
    # tracing just quietly stops doing whatever that one did. `common/` is
    # shared by every service here, so "nobody else registers one today" is a
    # fact with a short shelf life.
    #
    # Read what's already registered and pass it back through, making this
    # additive. Any future processor should be added the same way; if a second
    # one appears, hoist this read-append-configure into a shared helper rather
    # than repeating it.
    #
    # The aggregator is private, hence the getattr: if ddtrace renames it on
    # upgrade we fall back to replace-the-list behaviour instead of raising
    # during service startup. The whole block is wrapped for the same reason —
    # configure() is public but not immune, and a signature change across a
    # major ddtrace bump would otherwise raise straight out of
    # PubSubEventBus.start() and take the service down at boot. Trading correct
    # error rates for a service that starts is not a trade worth making.
    try:
        aggregator = getattr(_dd_tracer, "_span_aggregator", None)
        existing = list(getattr(aggregator, "user_processors", None) or [])
        _dd_tracer.configure(trace_processors=[*existing, PubSubPullDeadlineFilter()])
    except Exception:
        # Broad on purpose — every failure mode here is one we'd rather absorb
        # than raise, and enumerating ddtrace's internal exception types would
        # be a guess that rots on upgrade. Warning, not debug: the symptom
        # (Pub/Sub pull timeouts still counted as errors) is confusing enough
        # that it needs to name itself in the logs.
        logger.warning(
            "Failed to register the Pub/Sub pull-timeout span filter; empty "
            "pull windows will keep reporting as errors in APM",
            exc_info=True,
        )
        # _installed deliberately left False so a later start() retries.
        return False

    _installed = True
    return True
