"""Unit tests for the MCP middleware perimeter (gateway-secret check),
home-tenant prepend on the readable set, and gateway-only trust of the
identity headers.

The audit found ``MCPAuthMiddleware`` trusting ``X-Tenant-ID`` /
``X-Agent-ID`` / ``X-Readable-Tenant-IDs`` / ``X-Capabilities`` verbatim
with no ``X-Gateway-Secret`` validation — while REST ``get_auth_context``
refuses the header-trust path unless the gateway secret matches. Anyone
who could reach core-api's URL at ``/mcp`` directly could impersonate any
tenant. These tests pin the contract:

- secret configured + missing/wrong header → 401, downstream app never runs
- secret configured + correct header → request proceeds, tenant honored
- secret NOT configured (OSS / standalone / dev) → header trust unchanged
- readable set gets the home tenant prepended (parity with REST)
- identity headers are ignored on the non-gateway (direct) paths
"""

from __future__ import annotations

import json

import pytest

from core_api import mcp_server
from core_api.config import settings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def _reset_mcp_context_vars():
    """The session-scoped event loop shares one context across tests —
    leave the middleware's identity vars clean so values set here can't
    bleed into later tests in the run."""
    yield
    mcp_server._tenant_id_var.set(mcp_server._UNAUTH)
    mcp_server._agent_id_var.set(None)
    mcp_server._readable_tenant_ids_var.set(None)
    mcp_server._scopes_var.set(None)
    mcp_server._via_gateway_var.set(False)
    mcp_server._org_read_only_var.set(False)


async def _call_middleware(headers: list[tuple[bytes, bytes]]):
    """Invoke ``MCPAuthMiddleware`` once with a synthetic ASGI scope.

    Returns ``(app_called, sends)`` so callers can assert both the
    context-var side effects and whether the request was short-circuited
    with a response before reaching the downstream app.
    """
    called = {"app": False}

    async def _noop_app(scope, receive, send):
        called["app"] = True

    async def _recv():
        return {"type": "http.request", "body": b"", "more_body": False}

    sends: list[dict] = []

    async def _send(message):
        sends.append(message)

    mw = mcp_server.MCPAuthMiddleware(_noop_app)
    scope = {"type": "http", "headers": headers}
    await mw(scope, _recv, _send)
    return called["app"], sends


# ---------------------------------------------------------------------------
# S1 — gateway-secret perimeter
# ---------------------------------------------------------------------------


async def test_tenant_header_rejected_without_secret(monkeypatch):
    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    mcp_server._tenant_id_var.set(mcp_server._UNAUTH)

    app_called, sends = await _call_middleware(
        [
            (b"x-tenant-id", b"victim-tenant"),
            (b"x-capabilities", b"read,write"),
        ]
    )

    assert app_called is False
    start = next(m for m in sends if m["type"] == "http.response.start")
    assert start["status"] == 401
    body = next(m for m in sends if m["type"] == "http.response.body")
    payload = json.loads(body["body"])
    assert payload["error"]["code"] == "UNAUTHORIZED"
    # The spoofed tenant must not have been honored.
    assert mcp_server._get_tenant() != "victim-tenant"


async def test_tenant_header_rejected_with_wrong_secret(monkeypatch):
    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    mcp_server._tenant_id_var.set(mcp_server._UNAUTH)

    app_called, sends = await _call_middleware(
        [
            (b"x-tenant-id", b"victim-tenant"),
            (b"x-gateway-secret", b"guess"),
        ]
    )

    assert app_called is False
    start = next(m for m in sends if m["type"] == "http.response.start")
    assert start["status"] == 401
    assert mcp_server._get_tenant() != "victim-tenant"


async def test_tenant_header_honored_with_correct_secret(monkeypatch):
    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")

    app_called, sends = await _call_middleware(
        [
            (b"x-tenant-id", b"tenant-A"),
            (b"x-gateway-secret", b"s3cret"),
        ]
    )

    assert app_called is True
    assert not any(m["type"] == "http.response.start" for m in sends)
    assert mcp_server._get_tenant() == "tenant-A"


async def test_tenant_header_honored_when_secret_unset(monkeypatch):
    """OSS / standalone / dev deployments don't configure the shared
    secret — the header-trust path must keep working there (no-op
    perimeter, same as REST)."""
    monkeypatch.setattr(settings, "gateway_shared_secret", None)

    app_called, _ = await _call_middleware([(b"x-tenant-id", b"tenant-A")])

    assert app_called is True
    assert mcp_server._get_tenant() == "tenant-A"


# ---------------------------------------------------------------------------
# M1 — home-tenant prepend on the readable set (parity with REST)
# ---------------------------------------------------------------------------


async def test_readable_set_prepends_home_tenant(monkeypatch):
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    mcp_server._readable_tenant_ids_var.set(None)

    await _call_middleware(
        [
            (b"x-tenant-id", b"tenant-A"),
            (b"x-readable-tenant-ids", b"tenant-B,tenant-C"),
        ]
    )
    assert mcp_server._get_readable_tenants() == ["tenant-A", "tenant-B", "tenant-C"]


async def test_readable_set_does_not_duplicate_home_tenant(monkeypatch):
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    mcp_server._readable_tenant_ids_var.set(None)

    await _call_middleware(
        [
            (b"x-tenant-id", b"tenant-A"),
            (b"x-readable-tenant-ids", b"tenant-A,tenant-B"),
        ]
    )
    assert mcp_server._get_readable_tenants() == ["tenant-A", "tenant-B"]


# ---------------------------------------------------------------------------
# Identity headers are gateway-only — ignored on the direct paths
# ---------------------------------------------------------------------------


async def test_identity_headers_ignored_without_tenant_header(monkeypatch):
    """On the direct (non-gateway) paths a client must not be able to
    self-assert a cross-tenant read set, an agent identity, or a
    capability set by sending the gateway headers itself."""
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    # Pin non-standalone so the unknown-key path resolves to _UNAUTH
    # instead of requiring init_standalone() (conftest sets IS_STANDALONE).
    monkeypatch.setattr(settings, "is_standalone", False)
    mcp_server._readable_tenant_ids_var.set(None)
    mcp_server._scopes_var.set(None)
    mcp_server._agent_id_var.set(None)

    await _call_middleware(
        [
            (b"x-api-key", b"some-key"),
            (b"x-agent-id", b"spoofed-agent"),
            (b"x-readable-tenant-ids", b"tenant-B,tenant-C"),
            (b"x-capabilities", b"read,write"),
        ]
    )

    assert mcp_server._get_agent_id() is None
    assert mcp_server._get_readable_tenants() == []
    assert mcp_server._get_scopes() is None


# ---------------------------------------------------------------------------
# S2 — plan-limit read-only mode (X-Org-Read-Only)
#
# The gateway computes "over plan" from the persisted counters and stamps this
# header; REST turns it into a 403 via ``AuthContext.is_read_only``. The MCP
# middleware never read it, so no MCP tool could see plan-limit mode at all
# (caura-ai/caura#1205).
#
# The risk here runs the OPPOSITE way to the identity headers above. There,
# self-assertion buys access; here, a direct caller simply OMITTING the header
# would clear its own read-only flag. Both are closed by the same rule: the
# value is honoured only on the gateway-verified path.
# ---------------------------------------------------------------------------


async def test_org_read_only_is_honored_on_the_gateway_path(monkeypatch):
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    await _call_middleware(
        [(b"x-tenant-id", b"tenant-A"), (b"x-org-read-only", b"true")]
    )
    assert mcp_server._is_org_read_only() is True


async def test_org_read_only_is_ignored_off_the_gateway_path(monkeypatch):
    """A direct caller must not be able to SET one either."""
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    monkeypatch.setattr(settings, "is_standalone", False)
    await _call_middleware(
        [(b"x-api-key", b"some-key"), (b"x-org-read-only", b"true")]
    )
    assert mcp_server._is_org_read_only() is False


async def test_org_read_only_does_not_bleed_between_requests(monkeypatch):
    """The billing-bypass case: a stale False would let an over-plan tenant
    through, a stale True would refuse a paying one. Both silent, so the var is
    assigned on every request rather than only when the header is present."""
    monkeypatch.setattr(settings, "gateway_shared_secret", None)
    await _call_middleware(
        [(b"x-tenant-id", b"tenant-A"), (b"x-org-read-only", b"true")]
    )
    assert mcp_server._is_org_read_only() is True

    await _call_middleware([(b"x-tenant-id", b"tenant-A")])
    assert mcp_server._is_org_read_only() is False


async def test_org_read_only_requires_the_gateway_secret_when_configured(monkeypatch):
    """Rejected before any context var is set — the perimeter runs first."""
    monkeypatch.setattr(settings, "gateway_shared_secret", "s3cret")
    app_called, _ = await _call_middleware(
        [(b"x-tenant-id", b"tenant-A"), (b"x-org-read-only", b"true")]
    )
    assert app_called is False
    assert mcp_server._is_org_read_only() is False
