"""Presence is liveness plus debounced advice, not one write per delivery."""

import asyncio
import time
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace

from caura_bus_adapter import sdk
from caura_bus_core import AgentConfig, Claim, Envelope
from caura_bus_core.collaboration import Presence


async def test_twenty_deliveries_in_one_second_write_presence_at_most_twice(monkeypatch):
    writes = []
    done = asyncio.Event()
    deliveries = [
        Claim(
            delivery_id=f"d{i}",
            lease_token="token",
            lease_expires_at="2030-01-01T00:00:00Z",
            attempt=1,
            envelope=Envelope.new(from_="b", to=["a"], body="work"),
        )
        for i in range(20)
    ]
    remaining = list(deliveries)

    class Bus:
        async def advertise(self, profile):
            writes.append((time.monotonic(), profile.status))

        async def claim(self):
            if remaining:
                return remaining.pop()
            await asyncio.Future()

        async def events(self, after=0):
            await asyncio.Future()
            yield {}

        async def settle(self, claim, action):
            assert action == "ack"
            if not remaining:
                done.set()

    @asynccontextmanager
    async def connect(_):
        yield Bus()

    monkeypatch.setattr(sdk, "connected_bus", connect)
    consumed = []

    class Adapter(sdk.NoIdleGate):
        async def consume(self, envelope):
            await asyncio.sleep(0.02)
            consumed.append(envelope.id)

    config = AgentConfig(api_url="http://test", agent={"agent_id": "a", "tenant_id": "t"})
    started = time.monotonic()
    task = asyncio.create_task(sdk.run_adapter(config, Adapter()))
    try:
        await asyncio.wait_for(done.wait(), 1)
        assert len(consumed) == 20 and time.monotonic() - started < 1
        assert len(writes) <= 2 and all(status == "ready" for _, status in writes)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    assert len(writes) <= 2 and writes[-1][1] == "offline"
    assert all(b[0] - a[0] >= 0.99 for a, b in zip(writes, writes[1:], strict=False))


async def test_busy_and_ready_are_debounced_and_heartbeat_keeps_current_state():
    writes = []
    changed = asyncio.Event()

    async def advertise(profile):
        writes.append((time.monotonic(), profile.status))
        changed.set()

    profile = Presence(session_id="s", display_name="A")
    publisher = sdk.PresencePublisher(SimpleNamespace(advertise=advertise), profile)
    task = asyncio.create_task(publisher.run())
    try:
        await asyncio.wait_for(changed.wait(), 1)
        changed.clear()
        busy_at = time.monotonic()
        publisher.set_status("busy")
        await asyncio.sleep(0.1)
        assert [status for _, status in writes] == ["ready"]
        await asyncio.wait_for(changed.wait(), 2.5)
        assert writes[-1][1] == "busy" and writes[-1][0] - busy_at >= 1.99
        changed.clear()
        # A stable busy worker still advertises liveness every 15 seconds.
        await asyncio.wait_for(changed.wait(), 16)
        assert writes[-1][1] == "busy" and writes[-1][0] - writes[-2][0] >= 14.99
        changed.clear()
        ready_at = time.monotonic()
        publisher.set_status("ready")
        await asyncio.wait_for(changed.wait(), 3)
        assert writes[-1][1] == "ready" and writes[-1][0] - ready_at >= 1.99
        assert all(b[0] - a[0] >= 0.99 for a, b in zip(writes, writes[1:], strict=False))
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
