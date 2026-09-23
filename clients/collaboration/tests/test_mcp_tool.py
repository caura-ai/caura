"""Validate the public opcode tool boundary before any platform side effects."""

import json
from types import SimpleNamespace

import httpx
import pytest
from caura_bus_core import AgentConfig, Bus
from caura_bus_mcp.server import AppContext, mcp
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError


@pytest.fixture
async def tool(monkeypatch):
    requests = []

    async def handle(request):
        requests.append(request)
        path = request.url.path.removeprefix("/api/v1/bus")
        if path in {"/agents", "/discover"}:
            result = [{"agent_id": a} for a in ("a", "b", "c")]
        elif path == "/threads":
            result = [{"thread_id": "thread-1"}]
        elif path == "/messages" and request.method == "POST":
            body = json.loads(request.content)
            result = {"message_id": "m1", "thread_id": "thread-1", "recipients": body["to"]}
        elif path == "/messages":
            result = {"messages": [], "next_cursor": "5"}
        elif path == "/interventions":
            result = {"id": "case-1", "state": "pending"}
        else:
            result = {"deliveries": [{"state": "pending"}]}
        return httpx.Response(200, json=result)

    config = AgentConfig(
        api_url="https://caura.test", agent={"agent_id": "a", "tenant_id": "tenant"}, peers=["b"]
    )
    bus = Bus(config, api_key="test-key", transport=httpx.MockTransport(handle))
    context = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=AppContext(config, bus)))
    context = Context(request_context=context.request_context, mcp_server=mcp)

    async def call(payload):
        result = await mcp.call_tool("peer", payload, context=context)
        return json.loads(result.content[0].text)

    try:
        yield call, requests, config
    finally:
        await bus.close()


async def test_only_one_tool_is_advertised():
    tools = await mcp.list_tools()
    assert [t.name for t in tools] == ["peer"]
    schema = tools[0].input_schema
    assert schema["required"] == ["op"]
    assert set(schema["properties"]["op"]["enum"]) == {
        "discover",
        "send",
        "recent",
        "agents",
        "threads",
        "status",
        "human",
        "wait",
        "ack",
        "reply",
        "progress",
        "checkpoint",
        "memory_context",
    }


@pytest.mark.parametrize(
    "payload,method,path,field",
    [
        (
            {"op": "discover", "args": {"capability": "review", "available_only": False}},
            "GET",
            "/discover",
            "agents",
        ),
        ({"op": "agents", "args": {"fleet_id": "fleet"}}, "GET", "/agents", "agents"),
        (
            {"op": "send", "args": {"to": ["b"], "body": "Review", "idempotency_key": "stable"}},
            "POST",
            "/messages",
            "message_id",
        ),
        (
            {"op": "recent", "args": {"thread_id": "thread-1", "before": "8"}},
            "GET",
            "/messages",
            "messages",
        ),
        ({"op": "threads"}, "GET", "/threads", "threads"),
        ({"op": "status", "args": {"message_id": "m1"}}, "GET", "/messages/m1", "deliveries"),
        (
            {"op": "memory_context", "args": {"message_id": "m1"}},
            "GET",
            "/memory-context",
            "deliveries",
        ),
        (
            {"op": "memory_context", "args": {"delivery_id": "d1"}},
            "GET",
            "/memory-context",
            "deliveries",
        ),
        (
            {"op": "human", "args": {"delivery_id": "d1", "reason": "Conflicting results"}},
            "POST",
            "/interventions",
            "state",
        ),
    ],
)
async def test_opcodes_use_authenticated_platform_operations(tool, payload, method, path, field):
    call, requests, _ = tool
    result = await call(payload)
    assert field in result
    assert len(requests) == 1
    request = requests[0]
    assert request.method == method and request.url.path == "/api/v1/bus" + path
    assert request.headers["X-API-Key"] == "test-key"
    if payload["op"] == "discover":
        assert request.url.params["capability"] == "review"
        assert request.url.params["available_only"] == "false"
    elif payload["op"] == "agents":
        assert result["agents"] == [{"agent_id": "b"}, {"agent_id": "c"}]
        assert request.url.params["fleet_id"] == "fleet"
    elif payload["op"] == "send":
        assert request.headers["Idempotency-Key"] == "stable"
        assert json.loads(request.content)["body"] == "Review"
    elif payload["op"] == "recent":
        assert request.url.params["before"] == "8"
    elif payload["op"] == "human":
        assert json.loads(request.content) == payload["args"]


@pytest.mark.parametrize(
    "payload",
    [
        {"op": "delete"},
        {"op": "memory_context"},
        {"op": "memory_context", "args": {"message_id": "m1", "delivery_id": "d1"}},
        {"op": "memory_context", "args": {"message_id": "m1", "tenant_id": "other"}},
        {"op": "memory_context", "args": {"delivery_id": ""}},
        {"op": "send"},
        {"op": "send", "args": {"to": ["b"], "body": "Missing retry key"}},
        {"op": "human", "args": {"reason": "Missing delivery"}},
        {"op": "threads", "args": {"to": ["b"]}},
        {"op": "discover", "args": {"available_only": "false"}},
        {"op": "recent", "args": {"limit": 0}},
        {"op": "status", "args": {"message_id": "m1", "tenant_id": "other"}},
        {
            "op": "send",
            "args": {"to": ["b"], "body": "No parent", "kind": "response", "idempotency_key": "k"},
        },
    ],
)
async def test_invalid_calls_fail_before_platform_requests(tool, payload):
    call, requests, _ = tool
    with pytest.raises(ToolError):
        await call(payload)
    assert requests == []


async def test_recipient_allowlist_and_explicit_broadcast_survive_consolidation(tool):
    call, requests, config = tool
    args = {"to": ["c"], "body": "Hello", "idempotency_key": "k"}
    with pytest.raises(ToolError, match="allow-list"):
        await call({"op": "send", "args": args})
    assert requests == []
    args["to"] = ["*"]
    assert (await call({"op": "send", "args": args}))["recipients"] == ["b"]
    config.peers = ["*"]
    args["idempotency_key"] = "all"
    assert (await call({"op": "send", "args": args}))["recipients"] == ["b", "c"]


async def test_human_reply_keeps_parent_and_is_sent_to_platform_for_authorization(tool):
    call, requests, _ = tool
    await call(
        {
            "op": "send",
            "args": {
                "to": ["human:owner"],
                "body": "Reviewed",
                "kind": "response",
                "reply_to": "parent",
                "idempotency_key": "reply",
            },
        }
    )
    payload = json.loads(requests[0].content)
    assert payload["reply_to"] == "parent" and payload["to"] == ["human:owner"]


async def test_recent_forwards_response_filter_without_claiming(tool):
    call, requests, _ = tool
    result = await call({"op": "recent", "args": {"reply_to": "request-123"}})
    assert result["messages"] == []
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.params["reply_to"] == "request-123"
