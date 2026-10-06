"""MCP collect/recent present a response once; its queued delivery is still ACKed normally."""

import json
from types import SimpleNamespace

import httpx
import pytest
from caura_bus_core import AgentConfig, Bus
from caura_bus_mcp.server import AppContext, dispatch, mcp
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError

REQUEST = "msg_request"


def response(message_id, sender, body):
    return {
        "id": message_id,
        "from": sender,
        "to": ["a"],
        "kind": "response",
        "thread_id": "t1",
        "ts": 1,
        "body": body,
        "correlation_id": REQUEST,
    }


class Platform:
    """Stateful stand-in for the Caura bus routes this flow touches."""

    def __init__(self):
        self.messages = []
        self.deliveries = {
            r: {"id": f"d-{r}", "recipient": r, "state": "acked", "reply_state": "awaiting"}
            for r in ("b", "c")
        }
        self.queue = []  # response deliveries waiting for a's session
        self.requests = []

    def reply(self, sender, message_id, body):
        self.messages.append(response(message_id, sender, body))
        self.deliveries[sender].update(reply_state="replied", reply_message_id=message_id)
        self.queue.append(
            {
                "delivery_id": f"q-{message_id}",
                "lease_token": "secret",
                "lease_expires_at": "2030-01-01T00:00:00Z",
                "attempt": 1,
                "envelope": response(message_id, sender, body),
            }
        )

    async def handle(self, request):
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v1/bus/")
        if request.method == "GET" and path == "messages":
            reply_to = request.url.params.get("reply_to")
            rows = [m for m in reversed(self.messages) if not reply_to or m["correlation_id"] == reply_to]
            return httpx.Response(200, json={"messages": rows, "next_cursor": None})
        if request.method == "GET" and path == f"messages/{REQUEST}":
            return httpx.Response(200, json={"envelope": {}, "deliveries": list(self.deliveries.values())})
        if path == "inbox/wait":
            delivery = self.queue[0] if self.queue else None
            return httpx.Response(200, json={"delivery": delivery, "notices": []})
        if path.startswith("deliveries/") and path.endswith("/ack"):
            delivery_id = path.split("/")[1]
            self.queue = [d for d in self.queue if d["delivery_id"] != delivery_id]
            return httpx.Response(200, json={"state": "acked"})
        if path.startswith("deliveries/") and path.endswith("/observe"):
            delivery_id = path.split("/")[1]
            current = next((d for d in self.queue if d["delivery_id"] == delivery_id), None)
            state = {**current, "state": "leased"} if current else {"state": "acked"}
            return httpx.Response(200, json={"delivery": state | {"lease_token": "secret"}})
        if path.startswith("deliveries/") and path.endswith("/renew"):
            return httpx.Response(200, json={"lease_expires_at": "2030-01-01T00:00:00Z"})
        return httpx.Response(404, json={"detail": f"unexpected {request.method} {path}"})


@pytest.fixture
async def session():
    platform = Platform()
    config = AgentConfig(
        api_url="https://caura.test", agent={"agent_id": "a", "tenant_id": "tenant"}, peers=["b", "c"]
    )
    buses = []

    def start():
        bus = Bus(config, api_key="test-key", transport=httpx.MockTransport(platform.handle))
        buses.append(bus)
        app = AppContext(config, bus)
        context = Context(request_context=SimpleNamespace(lifespan_context=app), mcp_server=mcp)

        async def call(op, args=None):
            result = await mcp.call_tool("peer", {"op": op, "args": args or {}}, context=context)
            return json.loads(result.content[0].text)

        return call, app

    try:
        yield platform, start
    finally:
        for bus in buses:
            await bus.close()


async def test_collect_presents_once_and_queued_delivery_is_acked_normally(session):
    platform, start = session
    call, app = start()
    platform.reply("c", "r-c", "window: Tuesday")
    platform.reply("b", "r-b", "code: 7731")
    collected = await call("collect", {"message_id": REQUEST, "timeout": 1})
    assert collected["outcome"] == "complete" and collected["expected"] == ["b", "c"]
    assert [(a["recipient"], a["body"]) for a in collected["answers"]] == [
        ("b", "code: 7731"),
        ("c", "window: Tuesday"),
    ]
    # Collection is read-only: nothing was claimed or ACKed.
    assert all(r.method == "GET" for r in platform.requests)

    for message_id in ("r-c", "r-b"):
        waited = await call("wait", {"timeout": 0})
        delivery = waited["delivery"]
        assert delivery["envelope"]["id"] == message_id
        assert delivery["already_presented"] is True and delivery["envelope"]["body"] is None
        assert "lease_token" not in delivery
        await call("ack", {"delivery_id": delivery["delivery_id"]})
    assert platform.queue == []
    assert (await call("wait", {"timeout": 0}))["delivery"] is None
    await app.delivery.close()


async def test_recent_reply_to_marks_responses_presented(session):
    platform, start = session
    call, app = start()
    platform.reply("b", "r-b", "code: 7731")
    recent = await call("recent", {"reply_to": REQUEST})
    assert [m["id"] for m in recent["messages"]] == ["r-b"]
    assert platform.requests[-1].url.params["reply_to"] == REQUEST
    waited = await call("wait", {"timeout": 0})
    assert waited["delivery"]["already_presented"] is True
    await call("ack", {"delivery_id": waited["delivery"]["delivery_id"]})
    assert platform.queue == []
    await app.delivery.close()


async def test_wait_first_then_collect_does_not_repeat_the_body(session):
    platform, start = session
    call, app = start()
    platform.reply("b", "r-b", "code: 7731")
    waited = await call("wait", {"timeout": 0})
    assert waited["delivery"]["envelope"]["body"] == "code: 7731"
    assert "already_presented" not in waited["delivery"]
    await call("ack", {"delivery_id": waited["delivery"]["delivery_id"]})
    collected = await call("collect", {"message_id": REQUEST, "timeout": 0})
    assert collected["outcome"] == "partial"
    assert collected["answers"] == [
        {"recipient": "b", "sender": "b", "message_id": "r-b", "late": False, "already_presented": True}
    ]
    assert list(collected["pending"]) == ["c"] and "No reply yet from c" in collected["summary"]
    await app.delivery.close()


async def test_late_answer_collected_again_shows_only_the_new_body(session):
    platform, start = session
    call, app = start()
    platform.reply("b", "r-b", "code: 7731")
    first = await call("collect", {"message_id": REQUEST, "timeout": 0})
    assert first["outcome"] == "partial" and first["answers"][0]["body"] == "code: 7731"
    platform.reply("c", "r-c", "window: Tuesday")
    second = await call("collect", {"message_id": REQUEST, "timeout": 0})
    assert second["outcome"] == "complete"
    by_recipient = {a["recipient"]: a for a in second["answers"]}
    assert by_recipient["b"].get("already_presented") is True and "body" not in by_recipient["b"]
    assert by_recipient["c"]["body"] == "window: Tuesday"
    await app.delivery.close()


async def test_restart_forgets_presentation_and_shows_the_queued_response_again(session):
    platform, start = session
    call, app = start()
    platform.reply("b", "r-b", "code: 7731")
    await call("collect", {"message_id": REQUEST, "timeout": 0})
    await app.delivery.close()
    # A new MCP process is a new session: bookkeeping is in memory by design.
    restarted, app = start()
    waited = await restarted("wait", {"timeout": 0})
    assert waited["delivery"]["envelope"]["body"] == "code: 7731"
    assert "already_presented" not in waited["delivery"]
    await restarted("ack", {"delivery_id": waited["delivery"]["delivery_id"]})
    await app.delivery.close()


async def test_collect_rejects_host_unsafe_timeouts_and_remote_transport(session):
    _, start = session
    call, app = start()
    with pytest.raises(ToolError):
        await call("collect", {"message_id": REQUEST, "timeout": 50})
    with pytest.raises(ValueError, match="stdio transport"):
        await dispatch(app, "collect", {"message_id": REQUEST}, leased=False)
