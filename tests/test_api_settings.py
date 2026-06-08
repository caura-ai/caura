"""E2E tenant settings CRUD tests through HTTP API — real DB, no mocks."""

from tests.conftest import get_test_auth, uid as _uid


async def _get_settings(client, tenant_id: str, headers: dict) -> dict:
    resp = await client.get(
        f"/api/v1/settings?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp.status_code == 200, f"GET settings failed: {resp.text}"
    return resp.json()


async def test_get_default_settings(client):
    """GET /api/settings returns the full schema with null overrides for an unconfigured tenant."""
    # Use a fresh tenant per test so sibling tests can't pollute state.
    tenant_id, headers = get_test_auth(tenant_id=f"test-tenant-{_uid()}")

    settings = await _get_settings(client, tenant_id, headers)

    # All default sections should be present with null values
    assert "enrichment" in settings
    assert "recall" in settings
    assert "embedding" in settings
    assert "security_audit" in settings
    assert settings["enrichment"]["provider"] is None
    assert settings["recall"]["provider"] is None
    assert settings["embedding"]["provider"] is None
    # security_audit opt-in defaults — unset in DB → None in display view
    assert settings["security_audit"]["schedule_enabled"] is None
    assert settings["security_audit"]["alerts_enabled"] is None


async def test_update_settings_persists(client):
    """PUT /api/v1/settings persists overrides and GET reflects them merged with defaults."""
    tenant_id, headers = get_test_auth(tenant_id=f"test-tenant-{_uid()}")

    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={
            "enrichment": {"provider": "gemini", "model": "gemini-2.0-flash"},
        },
        headers=headers,
    )
    assert resp.status_code == 200, f"PUT failed: {resp.text}"

    reloaded = await _get_settings(client, tenant_id, headers)
    assert reloaded["enrichment"]["provider"] == "gemini"
    assert reloaded["enrichment"]["model"] == "gemini-2.0-flash"
    # Other sections remain at defaults
    assert reloaded["embedding"]["provider"] is None
