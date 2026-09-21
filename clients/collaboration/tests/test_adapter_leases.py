import asyncio

import pytest
from caura_bus_adapter.sdk import process_delivery
from caura_bus_core import Claim, Envelope, PlatformError


@pytest.fixture
def claim():
    return Claim(
        delivery_id="d",
        lease_token="token",
        lease_expires_at="2030-01-01T00:00:00Z",
        attempt=1,
        envelope=Envelope.new(from_="a", to=["b"], body="hello"),
    )


class Bus:
    def __init__(self):
        self.actions = []

    async def settle(self, claim, action):
        self.actions.append(action)

    async def events(self, after=0):
        await asyncio.Future()
        yield


async def test_success_acknowledges_after_consume_and_renews(claim):
    bus = Bus()

    class Adapter:
        async def consume(self, env):
            await asyncio.sleep(0.06)
            assert "ack" not in bus.actions

    await process_delivery(bus, Adapter(), claim, lease_seconds=0.03)
    assert bus.actions[-1] == "ack"
    assert "renew" in bus.actions


async def test_failure_nacks_without_ack(claim):
    bus = Bus()

    class Adapter:
        async def consume(self, env):
            raise RuntimeError("runtime failed")

    await process_delivery(bus, Adapter(), claim)
    assert bus.actions == ["nack"]


async def test_shutdown_leaves_message_recoverable(claim):
    bus = Bus()
    started = asyncio.Event()

    class Adapter:
        async def consume(self, env):
            started.set()
            await asyncio.Future()

    task = asyncio.create_task(process_delivery(bus, Adapter(), claim))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not bus.actions


async def test_lease_loss_cancels_consume_and_never_acknowledges(claim):
    canceled = asyncio.Event()

    class FailingBus(Bus):
        async def settle(self, claim, action):
            self.actions.append(action)
            raise PlatformError(409, "lease lost")

    class Adapter:
        async def consume(self, env):
            try:
                await asyncio.Future()
            finally:
                canceled.set()

    bus = FailingBus()
    await process_delivery(bus, Adapter(), claim, lease_seconds=0.03)
    assert canceled.is_set()
    assert "ack" not in bus.actions


async def test_live_interrupt_confirms_pause_only_after_consumer_stops(claim):
    started = asyncio.Event()
    stopped = asyncio.Event()

    class LiveBus(Bus):
        async def events(self, after=0):
            assert after == claim.event_cursor
            await started.wait()
            yield {
                "event_type": "delivery.interrupt",
                "payload": {"delivery_id": claim.delivery_id},
            }

        async def settle(self, claim, action):
            assert stopped.is_set()
            self.actions.append(action)

    class Runtime:
        supports_interrupt = True

        async def consume(self, env):
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

    bus = LiveBus()
    await asyncio.wait_for(process_delivery(bus, Runtime(), claim), timeout=1)
    assert bus.actions == ["paused"]


async def test_resumed_delivery_contains_human_instructions(claim):
    claim.resume_context = {"action": "redirect", "instructions": "Use staging"}
    bus = Bus()

    class Runtime:
        async def consume(self, env):
            assert env.parts[-1] == {"type": "caura_human_decision", **claim.resume_context}

    await process_delivery(bus, Runtime(), claim)
    assert bus.actions == ["ack"]


async def test_interrupt_between_consume_and_ack_still_confirms_stopped(claim):
    class RacingBus(Bus):
        async def settle(self, claim, action):
            self.actions.append(action)
            if action == "ack":
                raise PlatformError(409, "delivery paused")

    class Runtime:
        supports_interrupt = True

        async def consume(self, env):
            pass

    bus = RacingBus()
    await process_delivery(bus, Runtime(), claim)
    assert bus.actions == ["ack", "paused"]
