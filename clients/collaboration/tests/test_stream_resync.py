"""stream.resync_required: retention removed history after the cursor.

The client must reload state over REST and continue from ``resume_after``,
never treat the event as unknown noise or silently carry on.
"""

import asyncio
import json

import httpx
import pytest
from caura_bus_adapter import sdk
from caura_bus_cli import runtime
from caura_bus_cli.main import app
from caura_bus_core import RESYNC_EVENT, AgentConfig, Bus, Claim, Envelope, PlatformError
from caura_bus_core.retry import Backoff
from typer.testing import CliRunner

STATE = {"pending": True, "active": False, "wait_generation": 1, "drain_generation": 0, "cursor": 60}


def config():
    return AgentConfig(api_url="https://caura.test", agent={"agent_id": "a", "tenant_id": "t"})


def resync(seq=50, requested=3):
    return {
        "seq": seq,
        "event_type": RESYNC_EVENT,
        "payload": {"reason": "retention", "requested_after": requested, "resume_after": seq},
    }


def sse(*events):
    return "".join(f"id: {e['seq']}\nevent: {e['event_type']}\ndata: {json.dumps(e)}\n\n" for e in events)


@pytest.fixture
def quick_backoff(monkeypatch):
    async def sleep(self):
        await asyncio.sleep(0)

    monkeypatch.setattr(Backoff, "sleep", sleep)


async def test_core_reloads_rest_state_and_resumes_after_the_watermark(quick_backoff):
    calls = []

    async def handle(request):
        calls.append((request.url.path, request.url.params.get("after")))
        if request.url.path.endswith("/inbox/state"):
            return httpx.Response(200, json=STATE)
        if request.url.params["after"] == "3":
            return httpx.Response(200, text=sse(resync(), {"seq": 51, "event_type": "message.available"}))
        return httpx.Response(200, text=sse({"seq": 52, "event_type": "message.available"}))

    bus = Bus(config(), api_key="fixture", transport=httpx.MockTransport(handle))
    stream = bus.events(after=3)
    try:
        first = await anext(stream)
        assert first["event_type"] == RESYNC_EVENT and first["state"] == STATE
        # The REST reload happens before the event reaches the consumer.
        assert calls == [("/api/v1/bus/events", "3"), ("/api/v1/bus/inbox/state", None)]
        assert (await anext(stream))["seq"] == 51
        assert (await anext(stream))["seq"] == 52
        assert calls[-1] == ("/api/v1/bus/events", "51")
    finally:
        await stream.aclose()
        await bus.close()


async def test_core_reconnects_from_the_watermark_not_the_expired_cursor(quick_backoff):
    after = []

    async def handle(request):
        if request.url.path.endswith("/inbox/state"):
            return httpx.Response(200, json=STATE)
        after.append(request.url.params["after"])
        if len(after) == 1:
            return httpx.Response(200, text=sse(resync()))
        return httpx.Response(200, text=sse({"seq": 55, "event_type": "message.available"}))

    bus = Bus(config(), api_key="fixture", transport=httpx.MockTransport(handle))
    stream = bus.events(after=3)
    try:
        assert (await anext(stream))["event_type"] == RESYNC_EVENT
        assert (await anext(stream))["seq"] == 55
        assert after == ["3", "50"]
    finally:
        await stream.aclose()
        await bus.close()


async def test_failed_reload_keeps_the_old_cursor_and_resyncs_again(quick_backoff, monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _delay: real_sleep(0))
    after, reloads = [], []

    async def handle(request):
        if request.url.path.endswith("/inbox/state"):
            reloads.append(1)
            # Three transient failures exhaust one reload's retries.
            return httpx.Response(503) if len(reloads) <= 3 else httpx.Response(200, json=STATE)
        after.append(request.url.params["after"])
        return httpx.Response(200, text=sse(resync()))

    bus = Bus(config(), api_key="fixture", transport=httpx.MockTransport(handle))
    stream = bus.events(after=3)
    try:
        event = await anext(stream)
        assert event["state"] == STATE
        # Nothing was yielded, and the cursor did not move, until a reload worked.
        assert after == ["3", "3"] and len(reloads) == 4
    finally:
        await stream.aclose()
        await bus.close()


async def test_revoked_credentials_during_reload_stop_the_stream(quick_backoff):
    async def handle(request):
        if request.url.path.endswith("/inbox/state"):
            return httpx.Response(403, json={"detail": "revoked"})
        return httpx.Response(200, text=sse(resync()))

    bus = Bus(config(), api_key="fixture", transport=httpx.MockTransport(handle))
    stream = bus.events(after=3)
    try:
        with pytest.raises(PlatformError) as exc:
            await anext(stream)
        assert exc.value.status == 403
    finally:
        await stream.aclose()
        await bus.close()


def test_cli_watch_reports_the_resync_and_prints_the_reloaded_state(monkeypatch):
    class WatchBus:
        def __init__(self, _config):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def events(self, after=0):
            assert after == 3
            yield {**resync(), "state": STATE}

    monkeypatch.setattr("caura_bus_cli.main.Bus", WatchBus)
    monkeypatch.setattr("caura_bus_cli.main.load_config", lambda _path: config())
    result = CliRunner().invoke(app, ["watch", "--after", "3"])
    assert result.exit_code == 0
    assert "removed by retention" in result.stderr
    assert json.loads(result.stdout)["state"] == STATE


async def test_waker_treats_resync_as_a_wake_and_reconciles_from_rest(tmp_path, monkeypatch):
    snapshots = [{**STATE, "pending": False, "cursor": 2}, STATE]
    release = asyncio.Event()

    class FakeBus:
        def __init__(self, _config):
            self.reads = 0

        async def connect(self):
            return {}

        async def inbox_state(self):
            self.reads += 1
            return snapshots[min(self.reads, len(snapshots)) - 1]

        async def advertise(self, profile):
            pass

        async def close(self):
            pass

        async def events(self, after):
            assert after == 2
            yield {**resync(), "state": STATE}
            await release.wait()
            raise PlatformError(403, "revoked")

    bus = FakeBus(config())
    monkeypatch.setattr(runtime, "Bus", lambda _: bus)
    sent = []

    async def emit(message=runtime.WAKE_TEXT):
        sent.append(message)

    assert RESYNC_EVENT in runtime.WAKE_EVENTS
    task = asyncio.create_task(runtime.run_waker(config(), "codex", runtime.WakeState(tmp_path / "s"), emit))
    for _ in range(100):
        if sent:
            break
        await asyncio.sleep(0.01)
    release.set()
    with pytest.raises(PlatformError):
        await task
    # The resync caused a REST reconcile, which found pending work and woke the runtime.
    assert bus.reads >= 2 and sent == [runtime.WAKE_TEXT]


@pytest.fixture
def claim():
    return Claim(
        delivery_id="d",
        lease_token="token",
        lease_expires_at="2030-01-01T00:00:00Z",
        attempt=1,
        event_cursor=3,
        envelope=Envelope.new(from_="a", to=["b"], body="hello"),
    )


async def test_adapter_checks_its_lease_when_an_interrupt_may_have_been_pruned(claim):
    stopped = asyncio.Event()

    class PrunedBus:
        actions: list[str] = []

        async def events(self, after=0):
            assert after == claim.event_cursor
            yield {**resync(), "state": STATE}
            await asyncio.Future()

        async def settle(self, claim, action):
            self.actions.append(action)
            if action == "renew":
                # The removed history held a human pause: the lease says so.
                raise PlatformError(409, {"state": "paused"})

    class Runtime:
        supports_interrupt = True

        async def consume(self, env):
            try:
                await asyncio.Future()
            finally:
                stopped.set()

    bus = PrunedBus()
    await asyncio.wait_for(sdk.process_delivery(bus, Runtime(), claim, lease_seconds=300), timeout=1)
    assert stopped.is_set()
    assert bus.actions == ["renew", "paused"]
