"""The User-Agent family counter and its two hooks (REST auth, MCP middleware)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core_api import mcp_server
from core_api.auth import get_admin_key, get_auth_context
from core_api.config import settings
from core_api.heartbeat import clients
from tests.conftest import get_admin_headers

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _counter():
    clients.disable()
    yield
    clients.disable()


@pytest.mark.parametrize(
    ("ua", "family"),
    [
        ("openclaw-plugin/2.21.0", "openclaw-plugin"),
        ("caura-client-python/1.0.2", "caura-client-python"),
        ("caura-client-node/1.0.2 node/22.1.0", "caura-client-node"),
        ("caura-rail-python/1.0.1 (py3.12)", "caura-rail-python"),
        ("Caura-Rail-Node/1.0.1", "caura-rail-node"),
        ("  caura-client-python/1.0.2", "caura-client-python"),
        ("python-httpx/0.27.0", "other"),
        ("Mozilla/5.0", "other"),
        ("caura-server/3.16.0", "other"),
        ("", "other"),
        (None, "other"),
    ],
)
def test_family_for(ua, family):
    assert clients.family_for(ua) == family
    assert family in clients.FAMILIES


def test_snapshot_has_every_family():
    assert set(clients.snapshot()) == set(clients.FAMILIES)
    assert list(clients.snapshot()) == list(clients.FAMILIES)


def test_record_is_a_noop_while_disabled():
    clients.record("caura-client-python/1.0.2")
    clients.record_mcp()
    assert all(v == 0 for v in clients.snapshot().values())


def test_record_counts_while_enabled():
    clients.enable()
    clients.record("caura-client-python/1.0.2")
    clients.record("caura-client-python/1.0.3")
    clients.record("curl/8.0")
    clients.record_mcp()
    snap = clients.snapshot()
    assert snap["caura-client-python"] == 2
    assert snap["other"] == 1
    assert snap["mcp"] == 1
    clients.reset()
    assert all(v == 0 for v in clients.snapshot().values())
    assert clients.is_enabled() is True


def test_snapshot_is_a_copy():
    clients.enable()
    snap = clients.snapshot()
    snap["mcp"] = 99
    assert clients.snapshot()["mcp"] == 0


# ── REST hook ────────────────────────────────────────────────────────────


async def test_rest_hook_counts_authenticated_requests(client):
    clients.enable()
    headers = {**get_admin_headers(), "User-Agent": "caura-client-python/1.0.2"}
    resp = await client.get("/api/v1/telemetry", headers=headers)
    assert resp.status_code == 200, resp.text
    resp = await client.get(
        "/api/v1/telemetry", headers={**get_admin_headers(), "User-Agent": "curl/8"}
    )
    assert resp.status_code == 200, resp.text
    snap = clients.snapshot()
    assert snap["caura-client-python"] == 1
    assert snap["other"] == 1


async def test_rest_hook_skips_rejected_requests(monkeypatch):
    clients.enable()
    monkeypatch.setattr(settings, "is_standalone", False)
    request = SimpleNamespace(headers={"user-agent": "caura-client-python/1.0.2"})
    with pytest.raises(HTTPException):
        await get_auth_context(request, None)
    assert clients.snapshot()["caura-client-python"] == 0


async def test_rest_hook_counts_the_admin_key_path(monkeypatch):
    clients.enable()
    monkeypatch.setattr(settings, "is_standalone", False)
    request = SimpleNamespace(headers={"user-agent": "caura-rail-node/1.0.1"})
    ctx = await get_auth_context(request, get_admin_key())
    assert ctx.is_admin is True
    assert clients.snapshot()["caura-rail-node"] == 1


# ── MCP hook ─────────────────────────────────────────────────────────────


@pytest.fixture
def _reset_mcp_context_vars():
    yield
    mcp_server._tenant_id_var.set(mcp_server._UNAUTH)
    mcp_server._agent_id_var.set(None)
    mcp_server._readable_tenant_ids_var.set(None)
    mcp_server._scopes_var.set(None)
    mcp_server._via_gateway_var.set(False)
    mcp_server._org_read_only_var.set(False)


async def _call_middleware(headers: list[tuple[bytes, bytes]]) -> bool:
    called = {"app": False}

    async def _noop_app(scope, receive, send):
        called["app"] = True

    async def _recv():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        pass

    mw = mcp_server.MCPAuthMiddleware(_noop_app)
    await mw({"type": "http", "headers": headers}, _recv, _send)
    return called["app"]


@pytest.mark.usefixtures("_reset_mcp_context_vars")
async def test_mcp_hook_counts_authenticated_requests(monkeypatch):
    clients.enable()
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    monkeypatch.setattr(settings, "is_standalone", False)
    admin_key = get_admin_key()
    assert admin_key
    assert await _call_middleware(
        [
            (b"x-api-key", admin_key.encode()),
            (b"user-agent", b"caura-client-python/1.0.2"),
        ]
    )
    snap = clients.snapshot()
    assert snap["mcp"] == 1
    # Counted by transport, not by User-Agent.
    assert snap["caura-client-python"] == 0


@pytest.mark.usefixtures("_reset_mcp_context_vars")
async def test_mcp_hook_skips_unauthenticated_requests(monkeypatch):
    clients.enable()
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    monkeypatch.setattr(settings, "is_standalone", False)
    await _call_middleware([(b"x-api-key", b"wrong-key")])
    await _call_middleware([])
    assert clients.snapshot()["mcp"] == 0
