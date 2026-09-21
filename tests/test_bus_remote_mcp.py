"""Remote peer is request-scoped and cannot own or renew a delivery."""

import json
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError

from core_api.bus_mcp import register_peer


@pytest.fixture
async def remote(monkeypatch):
    native = SimpleNamespace(
        _get_agent_id=lambda: "agent-a",
        _get_tenant=lambda: "tenant-a",
        _via_gateway_var=SimpleNamespace(get=lambda default: True),
    )
    import core_api

    monkeypatch.setattr(core_api, "mcp_server", native, raising=False)
    monkeypatch.setitem(sys.modules, "core_api.mcp_server", native)
    api = FastAPI()
    calls = []

    @api.get("/api/v1/bus/{path}")
    async def read(path: str, request: Request):
        calls.append((path, dict(request.headers)))
        if request.headers.get("x-gateway-secret") != "verified-secret":
            raise HTTPException(401)
        if request.headers.get("x-caura-credential-kind") != "agent_key":
            raise HTTPException(403)
        if path == "identity":
            return {"tenant_id": "tenant-a", "agent_id": "agent-a"}
        return []

    server = MCPServer("optional-peer-test")
    register_peer(api, server)

    async def call(op, headers=None, args=None):
        ctx = Context(
            request_context=SimpleNamespace(
                request=SimpleNamespace(
                    headers=headers
                    or {
                        "x-gateway-secret": "verified-secret",
                        "x-caura-credential-kind": "agent_key",
                    }
                )
            ),
            mcp_server=server,
        )
        return await server.call_tool("peer", {"op": op, "args": args}, context=ctx)

    return call, calls, native


@pytest.mark.parametrize("op", ["wait", "ack", "reply", "progress", "checkpoint"])
async def test_remote_lease_operations_explicitly_require_stdio(remote, op):
    call, calls, _ = remote
    with pytest.raises(ToolError, match="stdio"):
        await call(op)
    assert calls == []


async def test_remote_uses_only_current_request_identity(remote):
    call, calls, _ = remote
    result = await call("threads")
    assert json.loads(result.content[0].text) == {"threads": []}
    assert [path for path, _ in calls] == ["identity", "threads"]
    assert all("x-api-key" not in headers for _, headers in calls)
    with pytest.raises(ToolError):
        await call("threads", headers={"x-gateway-secret": "forged"})
    assert calls[-1][0] == "identity"
    assert calls[-1][1]["x-gateway-secret"] == "forged"


async def test_remote_rejects_non_gateway_or_non_agent_identity(remote):
    call, calls, native = remote
    native._via_gateway_var = SimpleNamespace(get=lambda default: False)
    with pytest.raises(ToolError, match="authentication"):
        await call("threads")
    assert calls == []
