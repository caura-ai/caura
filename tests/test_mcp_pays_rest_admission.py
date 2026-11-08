"""MCP tool calls pay the admission REST pays.

REST limits its write, search and model-calling routes with per-credential
budgets and holds a per-tenant slot while each runs. The ``/mcp`` mount is
outside the route decorators that do this, and ``call_tool`` did nothing in
their place: ``caura_write(items=[...])`` wrote 100 rows a call at any rate,
outside the bulk budget and the tenant's write slots, and recall, list and the
model-calling tools ran with no budget at all.

The HTTP tests drive ``tools/call`` through ``MCPAuthMiddleware``, as a client
does, so the bucket a call pays from is the one the middleware names. REST and
MCP calls from one test client share a client address, which is the bucket
standalone uses when no credential names one.
"""

from __future__ import annotations

import contextlib
import json

import pytest
from httpx import ASGITransport, AsyncClient
from mcp.server.transport_security import TransportSecuritySettings

import core_api.middleware.per_tenant_concurrency as ptc
from core_api import mcp_server
from core_api.config import settings
from core_api.middleware.per_tenant_concurrency import per_tenant_slot
from core_api.middleware.rate_limit import limiter
from tests._mcp_test_helpers import parse_envelope
from tests.conftest import uid

_MODERN = "2026-07-28"
_NS = "io.modelcontextprotocol"
_META = {
    f"{_NS}/protocolVersion": _MODERN,
    f"{_NS}/clientCapabilities": {},
    f"{_NS}/clientInfo": {"name": "caura-tests", "version": "0"},
}


@pytest.fixture
def limited():
    """The suite runs with the limiter off; these tests need it on, with
    empty buckets."""
    prev = limiter.enabled
    limiter.enabled = True
    limiter.reset()
    yield
    limiter.enabled = prev
    limiter.reset()


@pytest.fixture
def fresh_slots():
    """Slots are sized when first made, so a test that lowers a cap starts and
    ends with none."""
    ptc._reset_for_tests()
    yield
    ptc._reset_for_tests()


@contextlib.asynccontextmanager
async def _mcp_client():
    """An MCP app over the module's server, as ``test_mcp_dual_stack`` builds
    one: ``streamable_http_app`` replaces the server's session manager, so the
    original is put back afterwards."""
    lowlevel = mcp_server.mcp._lowlevel_server
    original = lowlevel._session_manager
    app = mcp_server.MCPAuthMiddleware(
        mcp_server.mcp.streamable_http_app(
            streamable_http_path="/",
            json_response=True,
            stateless_http=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )
    )
    try:
        async with (
            mcp_server.mcp.session_manager.run(),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
        ):
            yield c
    finally:
        lowlevel._session_manager = original


async def _call(c: AsyncClient, tool: str, arguments: dict, headers=None) -> dict:
    resp = await c.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments, "_meta": _META},
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": _MODERN,
            "Mcp-Method": "tools/call",
            "Mcp-Name": tool,
            **(headers or {}),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "result" in body, body
    return body["result"]


def _error_code(result: dict) -> str | None:
    """The envelope's code when the call was refused, else None."""
    if not result.get("isError"):
        return None
    return json.loads(result["content"][0]["text"]).get("error", {}).get("code")


async def _call_in_process(tool: str, arguments: dict) -> str | None:
    """The refusal code of an in-process ``call_tool``, else None.

    A call that is let through runs on ``mcp_env``'s stubs, which can answer
    with text that is not an envelope or raise; neither is a refusal.
    """
    try:
        result = await mcp_server.mcp.call_tool(tool, arguments)
        return parse_envelope(result).get("error", {}).get("code")
    except Exception:
        return None


def _item() -> dict:
    return {"content": f"admission probe {uid()}", "memory_type": "fact"}


async def test_an_mcp_batch_write_spends_the_rest_bulk_budget(
    client, limited, monkeypatch
):
    """Both bulk doors draw on one budget: two REST batches use it up, and the
    MCP batch after them is refused rather than writing 100 more rows."""
    monkeypatch.setattr(settings, "rate_limit_write_bulk", "2/hour")
    for _ in range(2):
        resp = await client.post(
            "/api/v1/memories/bulk",
            json={"tenant_id": "default", "agent_id": "bulk-probe", "items": [_item()]},
            headers={"X-Bulk-Attempt-Id": f"attempt-{uid()}"},
        )
        assert resp.status_code < 300, resp.text

    async with _mcp_client() as mcp_client:
        result = await _call(
            mcp_client, "caura_write", {"agent_id": "bulk-probe", "items": [_item()]}
        )

    assert _error_code(result) == "RATE_LIMITED", result


async def test_mcp_recall_spends_its_credentials_search_budget(
    client, limited, monkeypatch
):
    """Behind the gateway each credential has its own budget, named by the
    credential the gateway accepted."""
    monkeypatch.setattr(settings, "gateway_shared_secret", "gw")
    monkeypatch.setattr(settings, "rate_limit_search", "1/hour")
    tenant = f"tenant-{uid()}"

    def _as(credential: str) -> dict:
        return {
            "X-Tenant-ID": tenant,
            "X-Agent-ID": "recall-probe",
            "X-Gateway-Secret": "gw",
            "X-Caura-Credential-ID": credential,
        }

    query = {"query": "who runs fleet ops"}
    async with _mcp_client() as mcp_client:
        first = await _call(mcp_client, "caura_recall", query, _as("cred-1"))
        again = await _call(mcp_client, "caura_recall", query, _as("cred-1"))
        other = await _call(mcp_client, "caura_recall", query, _as("cred-2"))

    assert _error_code(first) is None, first
    assert _error_code(again) == "RATE_LIMITED", again
    assert _error_code(other) is None, other


async def test_mcp_insights_spend_the_rest_model_budget(client, limited):
    """The model-calling routes share one budget per credential, 10 a minute
    by default, and the MCP tools draw on it too. Content this short makes no
    model call, but each preview still spends the budget."""
    for _ in range(10):
        resp = await client.post(
            "/api/v1/ingest/preview", json={"tenant_id": "default", "content": "short"}
        )
        assert resp.status_code == 200, resp.text

    async with _mcp_client() as mcp_client:
        result = await _call(mcp_client, "caura_insights", {"focus": "stale"})

    assert _error_code(result) == "RATE_LIMITED", result


@pytest.mark.parametrize(
    "arguments",
    [{"content": "slot probe"}, {"items": [{"content": "slot probe"}]}],
    ids=["single", "batch"],
)
async def test_an_mcp_write_needs_one_of_the_tenants_write_slots(
    mcp_env, fresh_slots, monkeypatch, arguments
):
    """With every write slot of the tenant held, an MCP write is refused fast
    instead of running beside them."""
    mcp_env["service"]("create_memory").return_value = {"id": "m1"}
    mcp_env["service"]("create_memories_bulk").return_value = {"created": 1}
    monkeypatch.setattr(settings, "per_tenant_write_concurrency", 1)

    async with per_tenant_slot("write", mcp_env["tenant"]):
        code = await _call_in_process("caura_write", arguments)

    assert code == "RATE_LIMITED"


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [("caura_recall", {"query": "q"}), ("caura_list", {})],
    ids=["recall", "list"],
)
async def test_an_mcp_read_needs_one_of_the_tenants_search_slots(
    mcp_env, fresh_slots, monkeypatch, tool, arguments
):
    """Recall and list, as their REST twins (``POST /recall``, ``GET
    /memories``) do."""
    mcp_env["service"]("search_memories").return_value = []
    monkeypatch.setattr(settings, "per_tenant_search_concurrency", 1)

    async with per_tenant_slot("search", mcp_env["tenant"]):
        code = await _call_in_process(tool, arguments)

    assert code == "RATE_LIMITED"
