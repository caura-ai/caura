"""Tests for pure ASGI middleware and MCP Bearer auth.

Covers:
- SecurityHeadersMiddleware: headers on non-MCP, skipped on /mcp
- ResponseTimeMiddleware: X-Response-Time on non-MCP, skipped on /mcp
- MCPAuthMiddleware: Bearer token extraction
"""

import pytest

from tests.conftest import get_test_auth

pytestmark = pytest.mark.asyncio


# ── SecurityHeadersMiddleware ──


async def test_security_headers_on_api_routes(client):
    """Non-MCP routes should have all security headers."""
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.headers["strict-transport-security"] == "max-age=63072000; includeSubDomains; preload"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "content-security-policy" in resp.headers


def test_security_headers_absent_on_mcp():
    """``SecurityHeadersMiddleware`` skips MCP-path scopes by design
    (via ``is_mcp_path``) so streaming MCP responses don't get browser
    security headers injected. After CAURA-000-mcp-trailing-slash a
    GET /mcp reaches the FastMCP handler in-process — which crashes
    without the session manager (no FastAPI lifespan in TestClient),
    so we can't exercise this via ``client.get`` anymore. Verify the
    middleware's skip condition directly instead.
    """
    from core_api.constants import is_mcp_path

    assert is_mcp_path("/mcp") is True
    assert is_mcp_path("/mcp/") is True
    assert is_mcp_path("/mcp/anything") is True
    assert is_mcp_path("/api/v1/memories") is False


# ResponseTimeMiddleware was removed during OSS/Enterprise split
# (it depended on enterprise metrics_service).


# ── MCPAuthMiddleware: Bearer token ──


async def test_mcp_auth_middleware_bearer_extraction(client):
    """Bearer auth works on REST routes. (The legacy "MCP mount exists"
    half of this test was an HTTP-level GET /mcp; after the Stage 1
    no-redirect fix it now reaches the FastMCP handler which crashes
    without the session manager — covered structurally in
    ``test_mcp_mount_exists`` below.)
    """
    import uuid
    tenant_id, headers = get_test_auth()
    uid = uuid.uuid4().hex[:8]

    resp1 = await client.post("/api/v1/memories", json={
        "tenant_id": tenant_id,
        "agent_id": f"bearer-test-{uid}",
        "fleet_id": f"bearer-fleet-{uid}",
        "memory_type": "fact",
        "content": f"baseline write [{uid}]",
    }, headers=headers)
    assert resp1.status_code == 201


async def test_mcp_bearer_returns_tools(client):
    """MCP tool-descriptions endpoint should accept Bearer auth."""
    resp = await client.get(
        "/api/v1/tool-descriptions",
        headers={"Authorization": "Bearer dev-admin-key"},
    )
    # tool-descriptions is an API route that accepts admin key
    assert resp.status_code == 200


def test_mcp_mount_exists():
    """MCP endpoint mount + exact-/mcp route are both registered on
    the app router. Structural check (post Stage-1 the HTTP-level
    GET path goes through the FastMCP handler which needs a running
    session manager — unavailable in TestClient without lifespan).
    """
    from starlette.routing import Mount, Route

    from core_api.app import app

    mounts = [r for r in app.router.routes if isinstance(r, Mount) and r.path == "/mcp"]
    bare_routes = [r for r in app.router.routes if isinstance(r, Route) and r.path == "/mcp"]
    assert mounts, "MCP mount missing at /mcp"
    assert bare_routes, "Bare-/mcp Route missing — Stage-1 redirect would recur"
