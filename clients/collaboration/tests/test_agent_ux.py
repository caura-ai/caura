"""Agent-facing recovery surfaces found by real model hosts.

1. Reusing an idempotency key for a different message names a stable code and
   a recoverable message instead of an opaque conflict.
2. The stdio MCP server advertises presence while it runs, so an MCP-only agent
   is discoverable, and goes offline on shutdown.
3. A response returned by ``wait`` says which request it answers and whether
   this session sent it, so a stale reply is not mistaken for the answer.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from caura_bus_core import IDEMPOTENCY_KEY_REUSED, AgentConfig, Bus, PlatformError, SendMessage
from caura_bus_core.collaboration import Presence
from caura_bus_core.envelope import Envelope
from caura_bus_mcp import presence as presence_module
from caura_bus_mcp import server
from caura_bus_mcp.delivery import UNMATCHED_RESPONSE
from caura_bus_mcp.presence import (
    PresenceHeartbeat,
    heartbeat_interval,
    presence_enabled,
    presence_profile,
)
from caura_bus_mcp.server import AppContext, mcp
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError

REUSED = {
    "code": IDEMPOTENCY_KEY_REUSED,
    "message": "idempotency key already used for a different message; use a new key",
}


def config(**agent):
    return AgentConfig(
        api_url="https://caura.test",
        agent={"agent_id": "a", "tenant_id": "tenant", **agent},
        peers=["b"],
    )


# --- 1. idempotency key reuse -------------------------------------------------


def test_platform_error_exposes_code_and_message():
    error = PlatformError(409, REUSED)
    assert error.status == 409 and error.code == IDEMPOTENCY_KEY_REUSED
    assert str(error) == f"Caura returned 409: {IDEMPOTENCY_KEY_REUSED}: {REUSED['message']}"
    plain = PlatformError(409, "Caura operation conflicts with the current state")
    assert plain.code is None
    assert str(plain) == "Caura returned 409: Caura operation conflicts with the current state"
    # Structured details without a code keep their previous rendering.
    assert PlatformError(409, {"state": "paused"}).code is None
    assert "{'state': 'paused'}" in str(PlatformError(409, {"state": "paused"}))


async def test_send_surfaces_reused_key_without_retrying():
    calls = []

    async def handle(request):
        calls.append(request)
        return httpx.Response(409, json={"detail": REUSED})

    bus = Bus(config(), api_key="k", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(PlatformError) as caught:
            await bus.send(SendMessage(to=["b"], body="second", kind="request"), idempotency_key="k1")
    finally:
        await bus.close()
    assert caught.value.code == IDEMPOTENCY_KEY_REUSED
    assert len(calls) == 1  # 409 is final; only transient statuses retry.


async def test_mcp_send_reports_the_code_to_the_model():
    async def handle(request):
        if request.url.path.endswith("/messages"):
            return httpx.Response(409, json={"detail": REUSED})
        return httpx.Response(404, json={"detail": "unexpected"})

    bus = Bus(config(), api_key="k", transport=httpx.MockTransport(handle))
    app = AppContext(config(), bus)
    context = Context(request_context=SimpleNamespace(lifespan_context=app), mcp_server=mcp)
    try:
        with pytest.raises(ToolError) as caught:
            await mcp.call_tool(
                "peer",
                {"op": "send", "args": {"to": ["b"], "body": "x", "idempotency_key": "k1"}},
                context=context,
            )
    finally:
        await app.delivery.close()
        await bus.close()
    text = str(caught.value)
    assert IDEMPOTENCY_KEY_REUSED in text and "use a new key" in text
    assert "conflicts with the current state" not in text


# --- 2. MCP presence ------------------------------------------------------------


def test_presence_is_on_by_default_and_can_be_disabled():
    assert presence_enabled({})
    assert presence_enabled({"CAURA_BUS_MCP_PRESENCE": "1"})
    for off in ("0", "false", "No", " off "):
        assert not presence_enabled({"CAURA_BUS_MCP_PRESENCE": off})


def test_presence_profile_comes_from_agent_config():
    profile = presence_profile(config(description="Reviews code", capabilities=["review", "go"]))
    assert profile.status == "ready" and profile.display_name == "a"
    assert profile.description == "Reviews code" and profile.capabilities == ["go", "review"]
    assert profile.session_id.startswith("mcp-")
    assert presence_profile(config()).session_id != profile.session_id
    # Older configs without capabilities still load.
    assert presence_profile(config()).capabilities == []


def test_heartbeat_follows_the_server_ttl():
    assert heartbeat_interval({"ttl_seconds": 45}) == 15
    assert heartbeat_interval({"ttl_seconds": 90}) == 30
    for unusable in (None, {}, {"ttl_seconds": 0}, {"ttl_seconds": "45"}, {"ttl_seconds": True}):
        assert heartbeat_interval(unusable) == 15
    assert heartbeat_interval({"ttl_seconds": 1}) == 1


class FakePresenceBus:
    def __init__(self, failures=()):
        self.adverts: list[str] = []
        self.failures = list(failures)
        self.advertised = asyncio.Event()

    async def advertise(self, profile):
        self.adverts.append(profile.status)
        self.advertised.set()
        if self.failures:
            raise self.failures.pop(0)
        return {"ttl_seconds": 45}


async def test_heartbeat_repeats_ready_then_goes_offline(monkeypatch):
    sleeps = []
    real_sleep = asyncio.sleep

    async def fast_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(presence_module.asyncio, "sleep", fast_sleep)
    bus = FakePresenceBus()
    heartbeat = PresenceHeartbeat(bus, Presence(session_id="mcp-1", display_name="a"))
    heartbeat.start()
    while len(bus.adverts) < 3:
        await real_sleep(0)
    await heartbeat.stop()
    assert set(bus.adverts[:-1]) == {"ready"} and bus.adverts[-1] == "offline"
    assert sleeps and set(sleeps) == {15}
    assert heartbeat.task is not None and heartbeat.task.done()


async def test_heartbeat_retries_transient_failures(monkeypatch):
    async def no_sleep(_seconds):
        await asyncio.sleep(0)

    monkeypatch.setattr(presence_module.Backoff, "sleep", lambda self: no_sleep(0))
    bus = FakePresenceBus([httpx.ConnectError("down"), PlatformError(503, "unavailable")])
    heartbeat = PresenceHeartbeat(bus, Presence(session_id="mcp-1", display_name="a"))
    heartbeat.start()
    while len(bus.adverts) < 3:
        await asyncio.sleep(0)
    await heartbeat.stop()
    assert bus.adverts == ["ready", "ready", "ready", "offline"]


async def test_revoked_credentials_stop_presence_without_offline_write():
    bus = FakePresenceBus([PlatformError(403, "forbidden")])
    heartbeat = PresenceHeartbeat(bus, Presence(session_id="mcp-1", display_name="a"))
    heartbeat.start()
    await heartbeat.task
    await heartbeat.stop()
    assert bus.adverts == ["ready"] and heartbeat.revoked


def lifespan_platform():
    adverts = []

    async def handle(request):
        path = request.url.path.removeprefix("/api/v1/bus")
        if path == "/identity":
            return httpx.Response(200, json={"agent_id": "a", "tenant_id": "tenant"})
        if path == "/presence":
            adverts.append(json.loads(request.content))
            return httpx.Response(200, json={"ttl_seconds": 45})
        return httpx.Response(404, json={"detail": "unexpected " + path})

    return adverts, handle


@pytest.mark.parametrize("enabled", [True, False])
async def test_stdio_server_advertises_presence_while_running(monkeypatch, enabled):
    adverts, handle = lifespan_platform()
    cfg = config(capabilities=["review"])
    monkeypatch.setattr(server, "load_config", lambda: cfg)
    monkeypatch.setattr(server, "Bus", lambda c: Bus(c, api_key="k", transport=httpx.MockTransport(handle)))
    monkeypatch.setenv("CAURA_BUS_MCP_PRESENCE", "1" if enabled else "0")
    async with server.lifespan(mcp):
        for _ in range(50):
            if adverts:
                break
            await asyncio.sleep(0.01)
        running = [a["status"] for a in adverts]
    statuses = [a["status"] for a in adverts]
    if not enabled:
        assert statuses == []
        return
    assert running == ["ready"]
    assert statuses == ["ready", "offline"]
    assert adverts[0]["capabilities"] == ["review"] and adverts[0]["session_id"].startswith("mcp-")
    assert adverts[0]["session_id"] == adverts[1]["session_id"]


def test_no_presence_flag_disables_presence(monkeypatch):
    monkeypatch.setenv("CAURA_BUS_MCP_PRESENCE", "1")  # restored after the test
    monkeypatch.setattr(server.mcp, "run", lambda: None)
    server.main(["--no-presence"])
    assert not presence_enabled()


# --- 3. response correlation ------------------------------------------------------


class Inbox:
    """Serves queued responses through wait; records the requests sent."""

    def __init__(self):
        self.queue: list[dict] = []
        self.served: dict | None = None
        self.count = 0

    def respond(self, reply_to, body):
        envelope = Envelope.new(from_="b", to=["a"], body=body, kind="response", correlation_id=reply_to)
        self.count += 1
        self.queue.append(
            {
                "delivery_id": f"d{self.count}",
                "lease_token": "tok",
                "attempt": 1,
                "envelope": envelope.model_dump(by_alias=True),
                "state": "leased",
            }
        )

    async def handle(self, request):
        path = request.url.path.removeprefix("/api/v1/bus")
        if path == "/messages" and request.method == "POST":
            body = json.loads(request.content)
            return httpx.Response(
                202, json={"message_id": "m-new", "thread_id": "t1", "recipients": body["to"]}
            )
        if path == "/inbox/wait":
            self.served = self.queue.pop(0) if self.queue else None
            return httpx.Response(200, json={"delivery": self.served, "notices": []})
        if path.endswith("/observe"):
            return httpx.Response(200, json={"delivery": self.served})
        if path.endswith(("/renew", "/ack")):
            return httpx.Response(200, json={"status": "acked", "lease_expires_at": "x"})
        return httpx.Response(404, json={"detail": "unexpected " + path})


@pytest.fixture
async def peer():
    inbox = Inbox()
    bus = Bus(config(), api_key="k", transport=httpx.MockTransport(inbox.handle))
    app = AppContext(config(), bus)
    context = Context(request_context=SimpleNamespace(lifespan_context=app), mcp_server=mcp)

    async def call(op, args=None):
        payload = {"op": op} if args is None else {"op": op, "args": args}
        result = await mcp.call_tool("peer", payload, context=context)
        return json.loads(result.content[0].text)

    try:
        yield call, inbox, app
    finally:
        await app.delivery.close()
        await bus.close()


async def test_wait_marks_a_stale_response_as_unmatched(peer):
    call, inbox, app = peer
    inbox.respond("m-old-run", "answer to a question from yesterday")
    delivery = (await call("wait", {"timeout": 0}))["delivery"]
    assert delivery["correlation"] == {
        "reply_to": "m-old-run",
        "correlation_id": "m-old-run",
        "matches_sent_request": False,
    }
    assert delivery["note"] == UNMATCHED_RESPONSE
    assert delivery["envelope"]["body"] == "answer to a question from yesterday"
    await call("ack", {"delivery_id": delivery["delivery_id"]})


async def test_wait_marks_a_response_to_this_sessions_request_as_matched(peer):
    call, inbox, app = peer
    sent = await call("send", {"to": ["b"], "body": "question", "kind": "request", "idempotency_key": "q1"})
    assert sent["message_id"] in app.delivery.sent_requests
    inbox.respond(sent["message_id"], "the answer")
    delivery = (await call("wait", {"timeout": 0}))["delivery"]
    assert delivery["correlation"]["reply_to"] == sent["message_id"]
    assert delivery["correlation"]["matches_sent_request"] is True
    assert "note" not in delivery


async def test_info_sends_and_non_responses_carry_no_correlation(peer):
    call, inbox, app = peer
    await call("send", {"to": ["b"], "body": "fyi", "idempotency_key": "i1"})
    assert app.delivery.sent_requests == set()
    inbox.respond(None, "x")
    inbox.queue[0]["envelope"]["kind"] = "request"
    delivery = (await call("wait", {"timeout": 0}))["delivery"]
    assert "correlation" not in delivery


async def test_prompt_surfaces_steer_answers_to_collect():
    (tool,) = await mcp.list_tools()
    text = " ".join(tool.description.split())
    assert "matches_sent_request" in text
    assert "Get answers with collect message_id=<request ID>" in text
    assert "never report an unmatched or old response as the answer" in text
    assert IDEMPOTENCY_KEY_REUSED in text


def test_template_steers_answers_to_collect_and_new_keys():
    template = " ".join(
        (
            Path(__file__).resolve().parents[3]
            / "docs"
            / "agent-collaboration"
            / "PEER_AGENT_CLAUDE_template.md"
        )
        .read_text()
        .split()
    )
    for phrase in (
        "this is the way to get answers",
        "`correlation.matches_sent_request`",
        "never report it as the answer",
        "`COLLABORATION_IDEMPOTENCY_KEY_REUSED` (409)",
        "`CAURA_BUS_MCP_PRESENCE=0`",
    ):
        assert phrase in template, phrase
