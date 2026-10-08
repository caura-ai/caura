"""Agents page through the directory instead of treating page one as all of it."""

import json
from types import SimpleNamespace

import httpx
import pytest
from caura_bus_core import AgentConfig, Bus, DirectoryIncomplete
from caura_bus_core.bus import DIRECTORY_PAGE_MAX
from caura_bus_mcp.server import DIRECTORY_TOOL_PAGE_MAX, AppContext, mcp, peer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError

# 90 registered agents; the only billing expert sorts far beyond page one.
NAMES = ["a"] + [f"peer-{i:03d}" for i in range(89)]
EXPERT = "peer-085"
SERVER_PAGE_CAP = 40  # The server may return fewer rows than requested.


def directory_server(requests, *, legacy=False, endless=False):
    def row(name):
        return {
            "agent_id": name,
            "description": "Owns billing and refunds." if name == EXPERT else None,
            "availability": "ready" if name == EXPERT else "offline",
            "capabilities": ["billing"] if name == EXPERT else [],
            "sessions": [],
        }

    async def handle(request):
        requests.append(request)
        path = request.url.path.removeprefix("/api/v1/bus")
        if request.method == "POST" and path == "/messages":
            body = json.loads(request.content)
            return httpx.Response(200, json={"message_id": "m1", "thread_id": "t1", "recipients": body["to"]})
        assert path in {"/agents", "/discover"}
        params = request.url.params
        rows = [row(n) for n in NAMES]
        if params.get("capability"):
            rows = [r for r in rows if params["capability"] in r["capabilities"]]
        if legacy or ("cursor" not in params and "limit" not in params):
            return httpx.Response(200, json=rows)
        # Mirror the server: an opaque cursor resumes strictly after a key.
        after = params.get("cursor", "").removeprefix("opaque:")
        rows = [r for r in rows if r["agent_id"] > after]
        size = min(int(params["limit"]), SERVER_PAGE_CAP)
        page = rows[:size]
        more = endless or len(rows) > size
        return httpx.Response(
            200, json={"agents": page, "next_cursor": "opaque:" + page[-1]["agent_id"] if more else None}
        )

    return handle


@pytest.fixture
async def directory():
    requests: list = []
    buses = []

    async def make(**server):
        config = AgentConfig(
            api_url="https://caura.test", agent={"agent_id": "a", "tenant_id": "tenant"}, peers=["*"]
        )
        bus = Bus(
            config, api_key="test-key", transport=httpx.MockTransport(directory_server(requests, **server))
        )
        buses.append(bus)
        context = Context(
            request_context=SimpleNamespace(lifespan_context=AppContext(config, bus)), mcp_server=mcp
        )

        async def call(payload):
            result = await mcp.call_tool("peer", payload, context=context)
            return json.loads(result.content[0].text)

        return call, bus

    try:
        yield make, requests
    finally:
        for bus in buses:
            await bus.close()


async def test_a_peer_beyond_the_first_page_is_discoverable(directory):
    make, requests = directory
    call, _ = await make()
    first = await call({"op": "discover", "args": {"available_only": False}})
    assert first["has_more"] is True and first["next_cursor"]
    assert EXPERT not in {a["agent_id"] for a in first["agents"]}
    found, page, pages = None, first, 1
    while found is None and page["has_more"]:
        page = await call(
            {"op": "discover", "args": {"available_only": False, "cursor": page["next_cursor"]}}
        )
        pages += 1
        found = next((a for a in page["agents"] if a["agent_id"] == EXPERT), None)
    assert found and found["description"] == "Owns billing and refunds."
    assert pages == 3  # 40-row server pages: peer-117 is on the third.
    # Every request was a bounded page request carrying the continuation.
    assert all(r.url.params["limit"] == "50" for r in requests)
    assert [r.url.params.get("cursor") for r in requests] == [None, first["next_cursor"], "opaque:peer-078"]
    # A filtered page answers directly when the filter runs before the bound.
    billing = await call({"op": "discover", "args": {"capability": "billing", "available_only": False}})
    assert [a["agent_id"] for a in billing["agents"]] == [EXPERT] and billing["has_more"] is False


async def test_agents_pages_exclude_self_and_reach_the_end(directory):
    make, _ = directory
    call, _ = await make()
    seen, page = [], {"next_cursor": None, "has_more": True}
    while page["has_more"]:
        args = {"limit": 100, **({"cursor": page["next_cursor"]} if page["next_cursor"] else {})}
        page = await call({"op": "agents", "args": args})
        seen += [a["agent_id"] for a in page["agents"]]
    assert seen == NAMES[1:] and len(seen) == len(set(seen))
    assert page["next_cursor"] is None


async def test_broadcast_expansion_follows_every_page(directory):
    make, requests = directory
    call, _ = await make()
    sent = await call({"op": "send", "args": {"to": ["*"], "body": "hello", "idempotency_key": "k"}})
    assert sent["recipients"] == NAMES[1:]
    pages = [r for r in requests if r.url.path.endswith("/agents")]
    assert len(pages) == 3 and all(r.url.params["limit"] == str(DIRECTORY_PAGE_MAX) for r in pages)


async def test_sdk_full_listing_and_page_bounds(directory):
    make, requests = directory
    _, bus = await make()
    assert [a["agent_id"] for a in await bus.agents_all()] == NAMES
    assert [a["agent_id"] for a in await bus.discover_all(available_only=False)] == NAMES
    count = len(requests)
    for bad in (0, -1, DIRECTORY_PAGE_MAX + 1, True):
        with pytest.raises(ValueError):
            await bus.agents_page(limit=bad)
        with pytest.raises(ValueError):
            await bus.discover_page(limit=bad)
    assert len(requests) == count  # rejected before any request


async def test_tool_page_size_is_bounded(directory):
    make, requests = directory
    call, _ = await make()
    for op in ("agents", "discover"):
        for args in (
            {"limit": 0},
            {"limit": DIRECTORY_TOOL_PAGE_MAX + 1},
            {"cursor": ""},
            {"cursor": "x" * 1025},
        ):
            with pytest.raises(ToolError):
                await call({"op": op, "args": args})
    assert requests == []


async def test_endless_directory_is_reported_not_truncated(directory):
    make, _ = directory
    _, bus = await make(endless=True)
    with pytest.raises(DirectoryIncomplete):
        await bus.agents_all(max_pages=3)


async def test_server_without_pagination_is_a_single_final_page(directory):
    make, _ = directory
    call, bus = await make(legacy=True)
    page = await call({"op": "agents"})
    assert page["has_more"] is False and page["next_cursor"] is None
    assert [a["agent_id"] for a in page["agents"]] == NAMES[1:]
    assert [a["agent_id"] for a in await bus.discover_all(available_only=False)] == NAMES


def test_tool_instructions_say_directory_results_may_be_incomplete():
    doc = peer.__doc__ or ""
    assert "INCOMPLETE" in doc and "cursor=next_cursor" in doc and "has_more" in doc
