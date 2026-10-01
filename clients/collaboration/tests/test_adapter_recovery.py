"""Adapters survive failed HTTP calls; revoked credentials still terminate them."""

import asyncio
from collections import Counter
from contextlib import suppress

import httpx
import pytest
from caura_bus_adapter import sdk
from caura_bus_core import AgentConfig, Bus, Claim, Envelope, PlatformError
from caura_bus_core.retry import Backoff


def config():
    return AgentConfig(api_url="https://caura.test", agent={"agent_id": "a", "tenant_id": "t"})


@pytest.fixture
async def quick_backoff(monkeypatch):
    sleeps = []

    async def sleep(self):
        sleeps.append(self)
        await asyncio.sleep(0)

    monkeypatch.setattr(Backoff, "sleep", sleep)
    return sleeps


@pytest.mark.parametrize("operation", ["identity", "presence", "inbox/claim", "events"])
@pytest.mark.parametrize("failure", [429, 500, 501, 502, 503, 504, 599, "connect", "read"])
async def test_adapter_recovers_transient_calls(operation, failure, monkeypatch, quick_backoff):
    calls = Counter()
    recovered = asyncio.Event()
    consumed = asyncio.Event()
    claimed = False
    claim = Claim(
        delivery_id="d",
        lease_token="token",
        lease_expires_at="2030-01-01T00:00:00Z",
        attempt=1,
        envelope=Envelope.new(from_="b", to=["a"], body="after restart"),
    )

    async def handle(request):
        nonlocal claimed
        route = request.url.path.removeprefix("/api/v1/bus/")
        calls[route] += 1
        if route == operation and calls[route] <= (3 if operation == "presence" else 1):
            if failure == "connect":
                raise httpx.ConnectError("restarting")
            if failure == "read":
                raise httpx.ReadError("upstream disappeared")
            return httpx.Response(failure, json={"detail": "unavailable"})
        if route == operation:
            recovered.set()
        if route == "identity":
            return httpx.Response(200, json={"agent_id": "a", "tenant_id": "t"})
        if route == "events":
            await asyncio.Future()
        if route == "inbox/claim":
            if not recovered.is_set() or claimed:
                await recovered.wait()
                if claimed:
                    await asyncio.Future()
            claimed = True
            return httpx.Response(200, json={"delivery": claim.model_dump(by_alias=True)})
        if route == "deliveries/d/ack":
            consumed.set()
        return httpx.Response(200, json={})

    bus = Bus(config(), api_key="fixture", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(sdk, "Bus", lambda _: bus)

    class Adapter(sdk.NoIdleGate):
        async def consume(self, env):
            assert recovered.is_set()
            assert env.body == "after restart"

    task = asyncio.create_task(sdk.run_adapter(config(), Adapter()))
    try:
        await asyncio.wait_for(consumed.wait(), 2)
        assert not task.done()
        assert calls[operation] >= 2
        assert quick_backoff
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("operation", ["identity", "presence", "inbox/claim", "events"])
@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_client_errors_are_fatal(operation, status, monkeypatch, quick_backoff):
    async def handle(request):
        route = request.url.path.removeprefix("/api/v1/bus/")
        if route == operation:
            return httpx.Response(status, json={"detail": "denied"})
        if route == "identity":
            return httpx.Response(200, json={"agent_id": "a", "tenant_id": "t"})
        await asyncio.Future()

    bus = Bus(config(), api_key="fixture", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(sdk, "Bus", lambda _: bus)
    # Shutdown presence is best effort and must not block the test.
    original = bus.advertise

    async def advertise(profile):
        if profile.status != "offline":
            return await original(profile)

    monkeypatch.setattr(bus, "advertise", advertise)
    with pytest.raises((PlatformError, ExceptionGroup)) as error:
        await asyncio.wait_for(sdk.run_adapter(config(), sdk.NoIdleGate()), 2)
    group = error.value
    if isinstance(group, ExceptionGroup):
        assert group.subgroup(lambda exc: isinstance(exc, PlatformError) and exc.status == status)
    else:
        assert group.status == status
    assert not quick_backoff


async def test_backoff_is_capped_jittered_and_resets(monkeypatch):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("caura_bus_core.retry.asyncio.sleep", sleep)
    monkeypatch.setattr("caura_bus_core.retry.secrets.randbelow", lambda _: 0)
    backoff = Backoff()
    for _ in range(100):
        await backoff.sleep()
    assert delays[:7] == [0.5, 1, 2, 4, 8, 15, 15]
    assert max(delays) == 15
    monkeypatch.setattr("caura_bus_core.retry.secrets.randbelow", lambda _: 1000)
    await backoff.sleep()
    assert delays[-1] == 30
    backoff.reset()
    await backoff.sleep()
    assert delays[-1] == 1


async def test_events_reconnect_with_last_durable_cursor(quick_backoff):
    cursors = []

    async def handle(request):
        cursors.append(request.url.params["after"])
        if len(cursors) == 2:
            return httpx.Response(500)
        seq = 7 if len(cursors) == 1 else 8
        return httpx.Response(200, text=f'data: {{"seq": {seq}, "event_type": "message.available"}}\n\n')

    bus = Bus(config(), api_key="fixture", transport=httpx.MockTransport(handle))
    stream = bus.events()
    try:
        assert (await anext(stream))["seq"] == 7
        assert (await anext(stream))["seq"] == 8
        assert cursors == ["0", "7", "7"]
    finally:
        await stream.aclose()
        await bus.close()
