"""Receipt and working acknowledgements use progress; one correlated reply carries the result.

The fake platform mirrors Caura's reply-state rule: the recipient's first correlated
reply, with or without ack, moves the sender's request to ``replied``; progress never does.
"""

import json
from pathlib import Path

import httpx
import pytest
from caura_bus_cli import runtime
from caura_bus_core import AgentConfig, Bus
from caura_bus_core.envelope import Envelope
from caura_bus_mcp.server import AppContext, dispatch, mcp

DOCS = Path(__file__).resolve().parents[3] / "docs" / "agent-collaboration"


class Platform:
    def __init__(self):
        self.request = Envelope.new(from_="architect", to=["developer"], body="Review", kind="request")
        self.reply_state = "awaiting"
        self.paths: list[str] = []
        self.progress_keys: set[str] = set()
        self.replies: dict[str, dict] = {}

    def delivery(self):
        return {
            "delivery_id": "d1",
            "lease_token": "token-1",
            "lease_expires_at": "2026-10-06T00:00:30+00:00",
            "attempt": 1,
            "state": "leased",
            "envelope": self.request.model_dump(by_alias=True),
        }

    async def handle(self, request):
        path = request.url.path.removeprefix("/api/v1/bus")
        self.paths.append(path)
        body = json.loads(request.content) if request.content else {}
        if path == "/inbox/wait":
            return httpx.Response(200, json={"delivery": self.delivery(), "notices": []})
        if path == "/deliveries/d1/observe":
            return httpx.Response(200, json={"delivery": self.delivery()})
        if path == "/deliveries/d1/renew":
            return httpx.Response(200, json={"lease_expires_at": "2026-10-06T00:01:00+00:00"})
        if path == "/deliveries/d1/progress":
            assert body["lease_token"] == "token-1"
            self.progress_keys.add(body["idempotency_key"])
            return httpx.Response(200, json={"status": "leased", "extension_count": len(self.progress_keys)})
        if path == "/deliveries/d1/reply":
            key = request.headers["Idempotency-Key"]
            receipt = self.replies.setdefault(
                key, {"message_id": f"reply-{len(self.replies) + 1}", "thread_id": self.request.thread_id}
            )
            self.reply_state = "replied"
            return httpx.Response(202, json={**receipt, "recipients": ["architect"]})
        if path == f"/messages/{self.request.id}":
            return httpx.Response(
                200, json={"deliveries": [{"recipient": "developer", "reply_state": self.reply_state}]}
            )
        return httpx.Response(404, json={"detail": "unexpected " + path})


@pytest.fixture
async def platform():
    fake = Platform()
    config = AgentConfig(
        api_url="https://caura.test",
        agent={"agent_id": "developer", "tenant_id": "tenant"},
        peers=["architect"],
    )
    bus = Bus(config, api_key="test-key", transport=httpx.MockTransport(fake.handle))
    app = AppContext(config, bus)
    try:
        yield fake, app
    finally:
        await app.delivery.close()
        await bus.close()


async def reply_state(fake, app):
    status = await dispatch(app, "status", {"message_id": fake.request.id})
    return status["deliveries"][0]["reply_state"]


async def test_progress_acknowledgements_keep_the_request_awaiting(platform):
    fake, app = platform
    claimed = await dispatch(app, "wait", {"timeout": 0})
    assert claimed["delivery"]["delivery_id"] == "d1"

    for key, summary in (("received", "Received; reviewing"), ("working", "Half the files reviewed")):
        await dispatch(app, "progress", {"delivery_id": "d1", "summary": summary, "idempotency_key": key})
        assert await reply_state(fake, app) == "awaiting"
    assert "/deliveries/d1/reply" not in fake.paths

    await dispatch(app, "reply", {"delivery_id": "d1", "body": "Findings: ...", "idempotency_key": "result"})
    assert await reply_state(fake, app) == "replied"
    assert fake.paths.count("/deliveries/d1/reply") == 1
    assert app.delivery.current is None


async def test_correlated_reply_without_ack_is_still_the_reply(platform):
    """ack=false keeps the lease, not the request: it must not be used as a receipt."""
    fake, app = platform
    await dispatch(app, "wait", {"timeout": 0})
    await dispatch(
        app,
        "reply",
        {"delivery_id": "d1", "body": "Got it", "idempotency_key": "early", "ack": False},
    )
    assert await reply_state(fake, app) == "replied"
    assert app.delivery.current is not None


async def test_instructions_direct_acknowledgements_to_progress():
    tools = await mcp.list_tools()
    description = " ".join(tools[0].description.split())
    assert "Use progress, never reply, to acknowledge receipt or report working status" in description
    assert "Send exactly one reply per delivery" in description
    assert "even ack=false, marks the sender's request replied" in description

    assert "Acknowledge with peer progress; send one reply with the result." in runtime.WAKE_TEXT

    template = " ".join((DOCS / "PEER_AGENT_CLAUDE_template.md").read_text().split())
    assert "acknowledge receipt with `progress`" in template
    assert "send exactly one correlated `reply` carrying the deliverable" in template
    assert '{"op":"progress","args":{"delivery_id":"<delivery_id>","summary":"Received' in template
    assert "Use `ack=false` for intermediate replies" not in template

    readme = " ".join((Path(__file__).resolve().parents[1] / "README.md").read_text().split())
    assert "Acknowledge receipt and report working status with `peer progress`" in readme
