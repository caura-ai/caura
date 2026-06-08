"""E2E ``/api/v1/whoami`` — identity probe for SDK bootstrap debugging."""

from __future__ import annotations


async def test_whoami_with_gateway_headers(client):
    # Gateway-routed path: X-Tenant-ID (+ optional X-Agent-ID) injected by
    # auth_validate → returned verbatim with auth_source=gateway-header.
    resp = await client.get(
        "/api/v1/whoami",
        headers={"X-Tenant-ID": "probe-tenant", "X-Agent-ID": "probe-agent"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["tenant_id"] == "probe-tenant"
    assert data["agent_id"] == "probe-agent"
    assert data["auth_source"] == "gateway-header"
    assert data["via_gateway"] is True


async def test_whoami_with_tenant_only(client):
    # mc_ tenant-key path: gateway sets X-Tenant-ID but no X-Agent-ID.
    resp = await client.get(
        "/api/v1/whoami",
        headers={"X-Tenant-ID": "probe-tenant"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tenant_id"] == "probe-tenant"
    assert data["agent_id"] is None
    assert data["auth_source"] == "gateway-header"


async def test_whoami_standalone_or_anonymous(client):
    # No gateway headers — tests run in standalone (auto-resolves) or
    # anonymous mode. Either way the endpoint must return 200 and a
    # structured envelope, never 401: it's a debug probe.
    resp = await client.get("/api/v1/whoami")
    assert resp.status_code == 200
    data = resp.json()
    assert data["via_gateway"] is False
    assert data["auth_source"] in {"standalone", "anonymous"}
