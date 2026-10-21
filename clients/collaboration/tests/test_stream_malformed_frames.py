"""A malformed live-event frame is a broken stream, not an event (L-240).

``Bus.events()`` must drop the connection and resume from the cursor it had,
as it does for a dropped connection, instead of ending the consumer with a
bare JSONDecodeError, AttributeError or KeyError.
"""

import asyncio
import json

import httpx
import pytest
from caura_bus_core import RESYNC_EVENT, AgentConfig, Bus
from caura_bus_core.retry import Backoff


def config():
    return AgentConfig(api_url="https://caura.test", agent={"agent_id": "a", "tenant_id": "t"})


def frame(data: str) -> str:
    return f"data: {data}\n\n"


@pytest.fixture
def quick_backoff(monkeypatch):
    async def sleep(self):
        await asyncio.sleep(0)

    monkeypatch.setattr(Backoff, "sleep", sleep)


@pytest.mark.parametrize(
    "data",
    [
        "<html>proxy error</html>",
        "[]",
        '"busy"',
        "null",
        json.dumps({"event_type": "message.available"}),
        json.dumps({"seq": 60, "event_type": RESYNC_EVENT, "payload": ["retention"]}),
        json.dumps({"seq": 60, "event_type": RESYNC_EVENT, "payload": {"resume_after": "later"}}),
    ],
)
async def test_malformed_frame_reconnects_from_the_last_good_cursor(quick_backoff, data):
    after, reloads = [], []

    async def handle(request):
        if request.url.path.endswith("/inbox/state"):
            reloads.append(1)
            return httpx.Response(200, json={})
        after.append(request.url.params["after"])
        if len(after) == 1:
            good = json.dumps({"seq": 51, "event_type": "message.available"})
            return httpx.Response(200, text=frame(good) + frame(data))
        return httpx.Response(200, text=frame(json.dumps({"seq": 52, "event_type": "message.available"})))

    bus = Bus(config(), api_key="fixture", transport=httpx.MockTransport(handle))
    stream = bus.events(after=3)
    try:
        assert (await anext(stream))["seq"] == 51
        assert (await anext(stream))["seq"] == 52
        # The bad frame went with its connection: the cursor stayed on the last
        # good event, and a malformed resync never triggered a state reload.
        assert after == ["3", "51"] and reloads == []
    finally:
        await stream.aclose()
        await bus.close()
