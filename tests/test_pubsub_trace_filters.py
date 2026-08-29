"""The Pub/Sub pull-timeout span filter must clear exactly that case.

Regression cover for the finding described in ``common/events/trace_filters``:
an idle subscriber's empty pull windows were arriving in APM as error spans,
drowning real failures — 51,568 of them from core-api in staging in 24h. The
risk in fixing it is over-reach — a filter that clears too much hides genuine
outages — so most of what follows pins down what must *stay* an error.

These tests deliberately do NOT require ddtrace. CI installs only each
service's ``[dev]`` extra, never ``datadog``, so a module-level
``importorskip("ddtrace")`` would skip this file in the one environment that
gates merges — shipping the filter with no coverage at all. The filter only
ever touches ``.error``, ``.resource`` and ``.get_tag()``, so a stub span
covers it, and ``_FIDELITY`` below pins the stub to the real Span shape
whenever the extra happens to be installed.
"""

from __future__ import annotations

import pytest

from common.events import trace_filters
from common.events.pubsub import PubSubEventBus
from common.events.trace_filters import (
    PULL_RESOURCE,
    PubSubPullDeadlineFilter,
)

# The key ddtrace records the gRPC status under. Equal to
# ``ddtrace.constants.ERROR_TYPE``; asserted in test_stub_matches_a_real_span.
ERROR_TYPE = "error.type"


class _StubSpan:
    """The subset of ddtrace's Span that the filter actually uses."""

    def __init__(
        self, resource: str, *, error: int, error_type: str | None = None
    ) -> None:
        self.resource = resource
        self.error = error
        self._tags: dict[str, str] = {}
        if error_type is not None:
            self._tags[ERROR_TYPE] = error_type

    def get_tag(self, key: str) -> str | None:
        return self._tags.get(key)


def _span(resource: str, *, error: int, error_type: str | None = None) -> _StubSpan:
    return _StubSpan(resource, error=error, error_type=error_type)


def test_clears_error_on_pull_deadline() -> None:
    span = _span(PULL_RESOURCE, error=1, error_type="StatusCode.DEADLINE_EXCEEDED")

    out = PubSubPullDeadlineFilter().process_trace([span])

    assert span.error == 0, "an empty pull window is not an error"
    # The span itself must survive: dropping it would also hide the real
    # failures this resource can produce, and lose poll timing.
    assert out is not None and span in out


def test_returns_trace_rather_than_dropping_it() -> None:
    """Returning None from process_trace discards the whole trace."""
    span = _span(PULL_RESOURCE, error=1, error_type="StatusCode.DEADLINE_EXCEEDED")
    other = _span("/some.other.Service/Call", error=0)

    out = PubSubPullDeadlineFilter().process_trace([span, other])

    assert out == [span, other]


@pytest.mark.parametrize(
    ("resource", "error_type", "why"),
    [
        (
            PULL_RESOURCE,
            "StatusCode.PERMISSION_DENIED",
            "a pull that is genuinely forbidden must stay an error",
        ),
        (
            PULL_RESOURCE,
            "StatusCode.NOT_FOUND",
            "a missing subscription halts the loop and must stay an error",
        ),
        (
            PULL_RESOURCE,
            "StatusCode.CANCELLED",
            # Observed for real alongside the timeouts: a pull interrupted by
            # shutdown. It says something true about the process, so it is not
            # this filter's business.
            "a cancelled pull is not a timeout and must stay an error",
        ),
        (
            "/google.pubsub.v1.Publisher/Publish",
            "StatusCode.DEADLINE_EXCEEDED",
            "a publish timeout loses an event and must stay an error",
        ),
        (
            "/google.pubsub.v1.Subscriber/StreamingPull",
            "StatusCode.DEADLINE_EXCEEDED",
            "only the exact resource the pull loop calls is corrected",
        ),
    ],
)
def test_leaves_other_errors_alone(resource: str, error_type: str, why: str) -> None:
    span = _span(resource, error=1, error_type=error_type)

    PubSubPullDeadlineFilter().process_trace([span])

    assert span.error == 1, why


def test_ignores_spans_that_are_not_errors() -> None:
    """A successful pull must not be touched, so nothing can be masked."""
    span = _span(PULL_RESOURCE, error=0)

    PubSubPullDeadlineFilter().process_trace([span])

    assert span.error == 0


def test_survives_a_missing_error_type_tag() -> None:
    """An error span with no error.type must not raise inside the filter.

    ddtrace wraps each processor in its own try/except, so a raise here would
    not lose traces — it would log.error on every single flush and silently
    stop correcting anything. Quiet-but-broken, which is the worse failure.
    """
    span = _span(PULL_RESOURCE, error=1)  # error set, tag absent

    PubSubPullDeadlineFilter().process_trace([span])

    assert span.error == 1, "no error.type means no evidence it was a timeout"


def test_stub_matches_a_real_span() -> None:
    """Guard against _StubSpan drifting from the thing it stands in for.

    Skipped in CI, which installs no `datadog` extra — the point is to catch
    drift locally or in a datadog-enabled build, so the stub-based tests above
    stay trustworthy.
    """
    pytest.importorskip("ddtrace", reason="only the `datadog` extra installs it")

    from ddtrace.constants import ERROR_TYPE as REAL_ERROR_TYPE
    from ddtrace.trace import tracer

    assert REAL_ERROR_TYPE == ERROR_TYPE, "the tag key the stub uses has moved"

    real = tracer.start_span("grpc")
    real.resource = PULL_RESOURCE
    real.error = 1
    real.set_tag(REAL_ERROR_TYPE, "StatusCode.DEADLINE_EXCEEDED")

    PubSubPullDeadlineFilter().process_trace([real])

    assert real.error == 0, "the filter must behave the same on a real Span"


class _FakeAggregator:
    def __init__(self, user_processors: list[object]) -> None:
        self.user_processors = user_processors


class _RecordingTracer:
    """Stands in for the global tracer so no test mutates real tracing.

    Models the one private attribute install() reads —
    ``_span_aggregator.user_processors`` — so the pass-through below is
    exercised against the real shape rather than a convenient one.
    """

    def __init__(self, already_registered: list[object] | None = None) -> None:
        self.calls: list[list[object]] = []
        self._span_aggregator = _FakeAggregator(already_registered or [])

    def configure(self, trace_processors: list[object]) -> None:
        self.calls.append(trace_processors)
        self._span_aggregator.user_processors = trace_processors


def _install_with(monkeypatch: pytest.MonkeyPatch, tracer_double: object) -> None:
    monkeypatch.setattr(trace_filters, "_dd_tracer", tracer_double)
    # Module-level, so it survives between tests and would leak the state of
    # whichever ran first; monkeypatch restores it either way.
    monkeypatch.setattr(trace_filters, "_installed", False)


@pytest.fixture
def fake_tracer(monkeypatch: pytest.MonkeyPatch) -> _RecordingTracer:
    tracer_double = _RecordingTracer()
    _install_with(monkeypatch, tracer_double)
    return tracer_double


def test_install_registers_the_filter(fake_tracer: _RecordingTracer) -> None:
    assert trace_filters.install() is True

    assert len(fake_tracer.calls) == 1
    (registered,) = fake_tracer.calls[0]
    assert isinstance(registered, PubSubPullDeadlineFilter)


def test_install_keeps_processors_registered_before_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """configure() replaces the processor list, so install() must re-pass it.

    Without this, registering our filter silently evicts anyone else's
    processor — no error, no warning, their filtering just stops. `common/` is
    shared by every service, so this needs to hold by construction.
    """
    someone_elses = object()
    tracer_double = _RecordingTracer(already_registered=[someone_elses])
    _install_with(monkeypatch, tracer_double)

    assert trace_filters.install() is True

    (registered,) = tracer_double.calls
    assert someone_elses in registered, "an existing processor must survive"
    assert any(isinstance(p, PubSubPullDeadlineFilter) for p in registered)
    assert len(registered) == 2


def test_install_survives_a_renamed_private_aggregator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pass-through reads a private attribute; an upgrade may rename it.

    Degrading to replace-the-list behaviour is acceptable. Raising during
    service startup, because APM internals moved, is not.
    """

    class _NoAggregator:
        def __init__(self) -> None:
            self.calls: list[list[object]] = []

        def configure(self, trace_processors: list[object]) -> None:
            self.calls.append(trace_processors)

    tracer_double = _NoAggregator()
    _install_with(monkeypatch, tracer_double)

    assert trace_filters.install() is True
    (registered,) = tracer_double.calls
    assert any(isinstance(p, PubSubPullDeadlineFilter) for p in registered)


def test_install_is_idempotent(fake_tracer: _RecordingTracer) -> None:
    """A second configure() would evict any other processor registered since.

    Every service constructs its own bus, and a process with two of them calls
    start() twice — so this guard is load-bearing, not hypothetical.
    """
    assert trace_filters.install() is True
    assert trace_filters.install() is True

    assert len(fake_tracer.calls) == 1, "configure() must be called exactly once"


def test_install_reports_false_without_ddtrace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-datadog-extra path: nothing to register."""
    monkeypatch.setattr(trace_filters, "_dd_tracer", None)
    monkeypatch.setattr(trace_filters, "_installed", False)

    assert trace_filters.install() is False


def test_install_absorbs_a_configure_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A broken configure() must not propagate out of PubSubEventBus.start().

    install() runs during service startup. Correct APM error rates are not
    worth trading a service that boots for, so a ddtrace incompatibility has
    to degrade rather than raise.
    """

    class _BrokenTracer:
        def configure(self, trace_processors: list[object]) -> None:
            raise TypeError("configure() got an unexpected keyword argument")

    _install_with(monkeypatch, _BrokenTracer())

    with caplog.at_level("WARNING"):
        assert trace_filters.install() is False

    assert "Failed to register" in caplog.text, "a silent failure is unusable"
    # Left False so a later start() can retry rather than being locked out by
    # one bad attempt.
    assert trace_filters._installed is False


async def test_publisher_only_bus_does_not_register_the_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No subscribers means no pull loop, so no pull spans to correct.

    Registration is not free — it calls tracer.configure(), which recreates
    the trace writer — and the comment at the call site claims this gating,
    so it needs to actually hold.
    """
    calls: list[bool] = []
    monkeypatch.setattr(trace_filters, "install", lambda: calls.append(True) or True)
    monkeypatch.setattr(
        PubSubEventBus, "_ensure_pubsub_sdk", staticmethod(lambda: object())
    )

    # dual on because ``lifecycle`` is flipped: the construction guard refuses
    # ``dual=False`` in this repo now. Irrelevant to what this asserts — a
    # publisher-only bus has no pull loop either way — so it takes the setting
    # the running services use rather than neutralising the guard.
    bus = PubSubEventBus(
        project_id="proj", subscription_prefix="test", dual_subscribe=True
    )
    await bus.start()  # publisher-only: subscribe() never called

    assert bus._started is True, "the bus must still start"
    assert calls == [], "a publisher-only bus has no pull loop to correct"
