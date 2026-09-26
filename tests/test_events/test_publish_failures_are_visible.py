"""A failed publish must produce a signal this platform can alert on.

``publish()`` deliberately does not block on the SDK future — blocking would
let a slow Pub/Sub wedge every caller, and the comment in ``publish`` explains
why at length. But the future was then discarded, so a publisher-side failure
(a 403 on the topic, an exhausted quota) produced log lines under a
``google.cloud`` logger name and nothing this platform watches, while
``is_healthy`` stayed true because it only ever examined the pull side.

These tests pin the signal, and pin that adding it did not reintroduce the
blocking it was careful to avoid.

Only ``test_a_failed_publish_is_logged_as_a_dropped_event`` distinguishes
before from after — verified failing with the future discarded again. The
other four pass either way by design: they guard the properties the fix had
to preserve (publish stays non-blocking, readiness does not flip) and the
ways it could have been wrong without failing (logging unconditionally,
raising on the SDK's thread).
"""

from __future__ import annotations

import logging
from concurrent.futures import Future
from unittest.mock import MagicMock

import pytest

from common.events import Event, PubSubEventBus, Topics

_TOPIC = Topics.Memory.EMBED_REQUESTED


@pytest.fixture()
def bus_and_future() -> tuple[PubSubEventBus, Future]:
    """A bus whose publisher hands back a REAL future.

    The shared fixture in ``test_pubsub_bus.py`` uses a MagicMock future,
    whose ``add_done_callback`` records the call and never invokes it — fine
    for asserting what was published, useless for asserting what happens when
    the publish fails.
    """
    bus = PubSubEventBus(project_id="proj", subscription_prefix="test")
    future: Future = Future()
    publisher = MagicMock(spec=["topic_path", "publish", "stop"])
    publisher.topic_path = lambda proj, topic: f"projects/{proj}/topics/{topic}"
    publisher.publish = MagicMock(return_value=future)
    bus._publisher = publisher
    return bus, future


def _event() -> Event:
    return Event(event_type=_TOPIC, tenant_id="t1", payload={"memory_id": "abc"})


async def test_a_failed_publish_is_logged_as_a_dropped_event(bus_and_future, caplog):
    bus, future = bus_and_future
    event = _event()

    with caplog.at_level(logging.ERROR, logger="common.events.pubsub"):
        await bus.publish(_TOPIC, event)
        future.set_exception(PermissionError("403 on topic"))

    dropped = [r for r in caplog.records if getattr(r, "dropped", None) is True]
    assert dropped, (
        "a publish that failed produced no app-level signal; the SDK's own "
        "logger is the only thing that saw it"
    )
    record = dropped[0]
    assert record.event_id == str(event.event_id)
    assert record.topic == _TOPIC


async def test_a_successful_publish_says_nothing(bus_and_future, caplog):
    """Vacuity guard: if the callback logged unconditionally, the test above
    would pass without the failure having anything to do with it."""
    bus, future = bus_and_future

    with caplog.at_level(logging.ERROR, logger="common.events.pubsub"):
        await bus.publish(_TOPIC, _event())
        future.set_result("msg-id-1")

    assert [r for r in caplog.records if getattr(r, "dropped", None) is True] == []


async def test_publish_still_returns_before_the_future_settles(bus_and_future):
    """The property the discarded future was protecting, and the reason this
    is a done-callback rather than an await: a slow or unavailable Pub/Sub
    must not hold a publish_concurrency thread — and admin-API request
    handlers await this call."""
    bus, future = bus_and_future

    await bus.publish(_TOPIC, _event())

    assert not future.done(), "publish() waited for the broker to confirm"


async def test_the_callback_does_not_raise_into_the_sdk_thread(bus_and_future):
    """It runs on the SDK's commit thread, where an exception is swallowed —
    which would make the failure MORE invisible, not less. Exercised directly
    with a future whose result() raises something unusual."""
    bus, future = bus_and_future

    await bus.publish(_TOPIC, _event())
    future.set_exception(BaseExceptionGroup("batch failed", [ValueError("x")]))

    # Reaching here at all is the assertion: set_exception invokes the
    # callback synchronously on this thread.
    assert future.done()


async def test_a_publish_failure_does_not_flip_the_readiness_flag(bus_and_future):
    """Deliberate, and the is_healthy docstring says so: this drives
    readiness, and draining a pod from the load balancer on a transient
    publish failure trades a lost event for an outage, and flaps."""
    bus, future = bus_and_future

    await bus.publish(_TOPIC, _event())
    future.set_exception(PermissionError("403 on topic"))

    assert bus.is_healthy is True
