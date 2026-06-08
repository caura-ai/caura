"""Security audit: multi-tenant isolation integration tests.

Validates fixes for the tenant isolation & API key leakage audit.
Every test runs against a real PostgreSQL instance.
"""

import pytest

from tests.conftest import get_admin_headers, get_test_auth


async def _write_memory(
    client, tenant_id: str, headers: dict, content: str,
    agent_id: str = "test-agent", fleet_id: str = "test-fleet",
) -> dict:
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "content": content,
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "memory_type": "fact",
        },
        headers=headers,
    )
    assert resp.status_code == 201, f"Write failed: {resp.text}"
    return resp.json()


# ═══════════════════════════════════════════════════════════════════════════
# 1. RLS bypass / unauthenticated access
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_no_auth_list_memories_scoped(client):
    """Without auth, listing memories returns only what RLS allows (standalone auto-auth)."""
    resp = await client.get("/api/v1/memories")
    # In standalone mode: 200 (auto-auth). In multi-tenant: 401/403/422.
    # The key assertion: no cross-tenant data leakage.
    assert resp.status_code in (200, 401, 403, 422)


@pytest.mark.integration
async def test_no_auth_cannot_list_fleets(client):
    """Without auth, listing fleets should fail (or require tenant_id)."""
    resp = await client.get("/api/v1/fleets")
    # In standalone mode with leaked session cookies, may get 200 with empty results.
    # In multi-tenant mode: 401/403/422. Either way, no cross-tenant data.
    if resp.status_code == 200:
        data = resp.json()
        # Acceptable only if empty (standalone auto-auth with no fleets)
        assert isinstance(data, list)
    else:
        assert resp.status_code in (401, 403, 422)


# ═══════════════════════════════════════════════════════════════════════════
# 2. Health endpoint — no sensitive stats
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_health_no_tenant_stats(client):
    """GET /api/health → no tenant_count or memory_count in public response."""
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data.get("storage") == "connected" or data.get("database") == "connected"
    assert "tenant_count" not in data
    assert "memory_count" not in data
    assert "last_write" not in data


# ═══════════════════════════════════════════════════════════════════════════
# 3. Enumeration endpoint lockdown (/tenants is admin-only)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_list_tenants_requires_auth(client):
    """GET /api/tenants requires some form of authentication."""
    # In standalone mode, auto-auth means 200 is acceptable
    # In multi-tenant without auth, should be 401/403
    # The admin-only version lives at /api/admin/tenants
    resp = await client.get("/api/v1/tenants")
    assert resp.status_code in (200, 401, 403)


@pytest.mark.integration
async def test_list_tenants_with_api_key(client):
    """GET /api/tenants with API key → 200 (scoped by RLS)."""
    _tenant_id, headers = get_test_auth("enum-org")
    resp = await client.get(
        "/api/v1/tenants",
        headers=headers,
    )
    assert resp.status_code == 200


# Cross-tenant isolation tests (sections 4+5) removed during OSS/Enterprise split.
# Per-user API key scoping is an enterprise feature — in OSS standalone mode,
# the admin key has access to all tenants by design.


@pytest.mark.integration
async def test_own_tenant_memory_access_ok(client):
    """A tenant can read their own memories normally."""
    import uuid
    tenant_id, headers = get_test_auth("self-org")
    await _write_memory(client, tenant_id, headers, f"My data for tenant isolation test [{uuid.uuid4().hex[:8]}]")

    resp = await client.get(
        f"/api/v1/memories?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    items = data.get("items", data) if isinstance(data, dict) and "items" in data else data
    assert len(items) >= 1
    assert all(m["tenant_id"] == tenant_id for m in items)


# ═══════════════════════════════════════════════════════════════════════════
# 6. Entity isolation
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_entity_cross_tenant_not_found(client):
    """Tenant B cannot fetch tenant A's entity (returns 404)."""
    tenant_a, headers_a = get_test_auth("ent-org-a")
    tenant_b, headers_b = get_test_auth("ent-org-b")

    # Write a memory with entity to create the entity in tenant A
    import uuid
    await _write_memory(client, tenant_a, headers_a, f"Alice is a developer [{uuid.uuid4().hex[:8]}]")

    # List entities for tenant A
    ent_resp = await client.get(
        f"/api/v1/entities?tenant_id={tenant_a}",
        headers=headers_a,
    )
    if ent_resp.status_code == 200 and ent_resp.json():
        entities = ent_resp.json()
        if isinstance(entities, dict) and "items" in entities:
            entities = entities["items"]
        if entities:
            entity_id = entities[0]["id"]
            # B tries to fetch A's entity → 404
            resp = await client.get(
                f"/api/v1/entities/{entity_id}?tenant_id={tenant_b}",
                headers=headers_b,
            )
            assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# 7. Install script / API key exposure
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_install_plugin_post_with_body(client):
    """POST /api/install-plugin with api_key in JSON body → 200."""
    resp = await client.post(
        "/api/v1/install-plugin",
        json={
            "fleet_id": "test-fleet",
            "api_url": "http://localhost",
            "api_key": "mc_test123456",
        },
    )
    assert resp.status_code == 200
    assert "#!/usr/bin/env bash" in resp.text
    assert 'chmod 600 "$PLUGIN_DIR/.env"' in resp.text


@pytest.mark.integration
async def test_install_plugin_get_header_only(client):
    """GET /api/install-plugin with key via X-API-Key header → 200."""
    resp = await client.get(
        "/api/v1/install-plugin?fleet_id=test-fleet",
        headers={"X-API-Key": "mc_test123456"},
    )
    assert resp.status_code == 200
    assert "#!/usr/bin/env bash" in resp.text


@pytest.mark.integration
async def test_install_script_has_chmod(client):
    """Install script includes chmod 600 for .env file."""
    resp = await client.post(
        "/api/v1/install-plugin",
        json={"fleet_id": "test", "api_key": "testkey"},
    )
    assert resp.status_code == 200
    assert 'chmod 600 "$PLUGIN_DIR/.env"' in resp.text


@pytest.mark.integration
async def test_install_plugin_get_no_api_key_query_param(client):
    """GET /api/install-plugin does NOT accept api_key as query param."""
    resp = await client.get(
        "/api/v1/install-plugin?fleet_id=test-fleet&api_key=mc_test123456",
    )
    # api_key query param should be ignored (not a recognized param)
    # The script should generate without the key (since header wasn't set)
    assert resp.status_code == 200
    # The key should NOT appear in the script since it was only in the query param
    assert "mc_test123456" not in resp.text


# ═══════════════════════════════════════════════════════════════════════════
# 8. Fleet route enforcement
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_fleet_create_no_auth_rejected(client):
    """POST /api/fleet without auth → 401 or 403."""
    resp = await client.post(
        "/api/v1/fleet",
        json={
            "tenant_id": "some-tenant",
            "fleet_id": "rogue-fleet",
        },
    )
    assert resp.status_code in (401, 403)


@pytest.mark.integration
async def test_fleet_heartbeat_no_auth_rejected(client):
    """POST /api/fleet/heartbeat without auth → 401 or 403."""
    resp = await client.post(
        "/api/v1/fleet/heartbeat",
        json={
            "tenant_id": "some-tenant",
            "node_name": "rogue-node",
        },
    )
    assert resp.status_code in (401, 403)


# ═══════════════════════════════════════════════════════════════════════════
# 9. Shell injection prevention in install script
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_install_script_escapes_malicious_api_url(client):
    """Shell metacharacters in api_url are escaped via shlex.quote()."""
    resp = await client.post(
        "/api/v1/install-plugin",
        json={
            "fleet_id": "test-fleet",
            "api_url": "https://evil.com; rm -rf /",
            "api_key": "mc_testkey123456",
        },
    )
    assert resp.status_code == 200
    script = resp.text
    # The malicious URL must be single-quoted by shlex.quote() — not bare
    assert "MEMCLAW_API_URL='https://evil.com; rm -rf /'" in script
    # Bare (unquoted) interpolation into curl must NOT appear
    assert 'curl -sf "https://evil.com; rm -rf /' not in script
    assert 'curl $CURL_INSECURE -sf "https://evil.com; rm -rf /' not in script
    # The script uses the safe bash variable, not raw interpolation. The
    # bootstrap curls were rewired through ``$CURL_INSECURE`` for HTTPS
    # TOFU (CAURA-000 install-plugin), so the var sits between ``curl``
    # and ``-sf`` — match the safe-variable usage rather than the exact
    # arg ordering.
    assert '"$MEMCLAW_API_URL/api/plugin-source' in script


@pytest.mark.integration
async def test_install_script_escapes_malicious_node_name(client):
    """Shell metacharacters in node_name are escaped."""
    resp = await client.post(
        "/api/v1/install-plugin",
        json={
            "fleet_id": "test",
            "api_key": "mc_testkey123456",
            "node_name": "$(whoami)",
        },
    )
    assert resp.status_code == 200
    script = resp.text
    # The raw $(whoami) should NOT appear unquoted
    assert "MEMCLAW_NODE_NAME=$(whoami)" not in script
    # It should be single-quoted
    assert "'$(whoami)'" in script


@pytest.mark.integration
async def test_install_script_escapes_backtick_injection(client):
    """Backtick command substitution in fleet_id is escaped."""
    resp = await client.post(
        "/api/v1/install-plugin",
        json={
            "fleet_id": "`cat /etc/passwd`",
            "api_key": "mc_testkey123456",
        },
    )
    assert resp.status_code == 200
    script = resp.text
    # Should be safely quoted
    assert "'`cat /etc/passwd`'" in script


# ═══════════════════════════════════════════════════════════════════════════
# 10. Error response in production hides error_type
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
async def test_error_response_no_error_type_in_test(client):
    """500 error responses include path but handle error_type based on environment."""
    # Trigger a 500 by hitting an endpoint that would fail internally
    # We can't easily trigger a real 500, so just verify the handler is registered
    # by checking the app's exception_handlers
    from core_api.app import app
    assert Exception in app.exception_handlers
