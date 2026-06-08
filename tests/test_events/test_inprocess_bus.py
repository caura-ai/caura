"""In-process event bus behaviour."""

from __future__ import annotations

import asyncio

import pytest

from common.events import (
    CircularPublishChainError,
    Event,
    InProcessEventBus,
    Topics,
)


async def test_publish_delivers_to_single_subscriber() -> None:
    bus = InProcessEventBus()
    received: list[Event] = []

    async def handler(event: Event) -> None:
        received.append(event)

    bus.subscribe(Topics.Memory.EMBED_REQUESTED, handler)
    event = Event(
        event_type=Topics.Memory.EMBED_REQUESTED,
        tenant_id="t1",
        payload={"memory_id": "abc"},
    )
    await bus.publish(Topics.Memory.EMBED_REQUESTED, event)
    await bus.drain()

    assert len(received) == 1
    assert received[0].event_id == event.event_id


async def test_publish_fans_out_to_multiple_subscribers() -> None:
    bus = InProcessEventBus()
    counts = {"a": 0, "b": 0}

    async def handler_a(_e: Event) -> None:
        counts["a"] += 1

    async def handler_b(_e: Event) -> None:
        counts["b"] += 1

    bus.subscribe(Topics.Memory.CREATED, handler_a)
    bus.subscribe(Topics.Memory.CREATED, handler_b)

    for _ in range(3):
        await bus.publish(
            Topics.Memory.CREATED,
            Event(event_type=Topics.Memory.CREATED, tenant_id="t1"),
        )
    await bus.drain()

    assert counts == {"a": 3, "b": 3}


async def test_no_subscribers_is_noop() -> None:
    bus = InProcessEventBus()
    # Publishing to a topic no one subscribes to must not raise.
    await bus.publish(
        Topics.Memory.CREATED,
        Event(event_type=Topics.Memory.CREATED),
    )
    await bus.drain()


async def test_handler_exception_does_not_break_other_subscribers() -> None:
    bus = InProcessEventBus()
    other_ran = asyncio.Event()

    async def bad_handler(_e: Event) -> None:
        raise RuntimeError("intentional test failure")

    async def good_handler(_e: Event) -> None:
        other_ran.set()

    bus.subscribe(Topics.Audit.EVENT_RECORDED, bad_handler)
    bus.subscribe(Topics.Audit.EVENT_RECORDED, good_handler)
    await bus.publish(
        Topics.Audit.EVENT_RECORDED,
        Event(event_type=Topics.Audit.EVENT_RECORDED),
    )
    await bus.drain()
    assert other_ran.is_set()


async def test_drain_awaits_in_flight_tasks() -> None:
    bus = InProcessEventBus()
    order: list[str] = []

    async def slow_handler(_e: Event) -> None:
        await asyncio.sleep(0.05)
        order.append("handler")

    bus.subscribe(Topics.Memory.EMBEDDED, slow_handler)
    await bus.publish(
        Topics.Memory.EMBEDDED, Event(event_type=Topics.Memory.EMBEDDED)
    )
    order.append("pre-drain")
    await bus.drain()
    order.append("post-drain")

    # Handler finished before drain returned.
    assert order == ["pre-drain", "handler", "post-drain"]


async def test_event_envelope_auto_populates_id_and_timestamp() -> None:
    e = Event(event_type=Topics.Memory.CREATED)
    assert e.event_id is not None
    assert e.occurred_at is not None


async def test_event_is_frozen() -> None:
    e = Event(event_type=Topics.Memory.CREATED)
    with pytest.raises((TypeError, ValueError)):
        e.event_type = "other"  # type: ignore[misc]


async def test_drain_raises_on_circular_publish_chain() -> None:
    # Handler A publishes B, B publishes A — without the max_rounds
    # guard, `drain()` would hang indefinitely.
    bus = InProcessEventBus()

    async def handler_a(_e: Event) -> None:
        await bus.publish(Topics.Memory.EMBEDDED, Event(event_type=Topics.Memory.EMBEDDED))

    async def handler_b(_e: Event) -> None:
        await bus.publish(Topics.Memory.CREATED, Event(event_type=Topics.Memory.CREATED))

    bus.subscribe(Topics.Memory.CREATED, handler_a)
    bus.subscribe(Topics.Memory.EMBEDDED, handler_b)

    await bus.publish(Topics.Memory.CREATED, Event(event_type=Topics.Memory.CREATED))
    with pytest.raises(CircularPublishChainError, match="circular event-publish chain"):
        await bus.drain(max_rounds=5)
    # drain() is responsible for cancelling the remaining cycle tasks
    # before raising; if it didn't, pytest would warn about "Task was
    # destroyed but it is pending".
    assert bus._tasks == set()


async def test_stop_reraises_non_circular_runtime_error() -> None:
    # Only the specific circular-chain RuntimeError should be swallowed.
    # Any other RuntimeError is a bug and must propagate.
    bus = InProcessEventBus()

    async def fake_drain(max_rounds: int = 100) -> None:
        raise RuntimeError("something else entirely")

    bus.drain = fake_drain  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="something else entirely"):
        await bus.stop()


async def test_stop_swallows_circular_chain_runtime_error() -> None:
    # `stop()` is called from shutdown paths (FastAPI lifespan,
    # signal handlers) and must never raise — a bad handler graph
    # shouldn't crash the shutdown sequence.
    bus = InProcessEventBus()

    async def cycle_a(_e: Event) -> None:
        await bus.publish(Topics.Memory.EMBEDDED, Event(event_type=Topics.Memory.EMBEDDED))

    async def cycle_b(_e: Event) -> None:
        await bus.publish(Topics.Memory.CREATED, Event(event_type=Topics.Memory.CREATED))

    bus.subscribe(Topics.Memory.CREATED, cycle_a)
    bus.subscribe(Topics.Memory.EMBEDDED, cycle_b)
    await bus.publish(Topics.Memory.CREATED, Event(event_type=Topics.Memory.CREATED))

    # Low max_rounds so the test completes quickly, and this must not raise.
    await bus.stop()
    assert bus._tasks == set()


async def test_is_healthy_default_true() -> None:
    """InProcessEventBus has no cross-service failure modes, so the
    ABC default (True) applies. Readiness probes that check
    ``bus.is_healthy`` should stay green regardless of publisher /
    subscriber state in-process."""
    bus = InProcessEventBus()
    assert bus.is_healthy is True
