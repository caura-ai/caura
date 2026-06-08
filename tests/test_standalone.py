"""Tests for standalone mode (IS_STANDALONE=true)."""

import pytest

from tests.conftest import uid as _uid

pytestmark = pytest.mark.asyncio


def _get_standalone_tenant() -> str:
    """Return the standalone tenant_id by calling init_standalone()."""
    from core_api.standalone import init_standalone
    return init_standalone()


# ---------------------------------------------------------------------------
# Standalone = True (default in tests)
# ---------------------------------------------------------------------------


async def test_standalone_list_no_auth(client):
    """List memories without auth headers in standalone mode."""
    tenant_id = _get_standalone_tenant()
    resp = await client.get(f"/api/v1/memories?tenant_id={tenant_id}")
    assert resp.status_code == 200


async def test_standalone_write_no_auth(client):
    """Write a memory without auth headers in standalone mode."""
    tenant_id = _get_standalone_tenant()
    tag = _uid()
    resp = await client.post("/api/v1/memories", json={
        "tenant_id": tenant_id,
        "content": f"Standalone write test [{tag}]",
        "agent_id": f"test-agent-{tag}",
        "fleet_id": f"test-fleet-{tag}",
    })
    assert resp.status_code == 201


async def test_standalone_search_no_auth(client):
    """Search without auth headers in standalone mode."""
    tenant_id = _get_standalone_tenant()
    resp = await client.post("/api/v1/search", json={
        "tenant_id": tenant_id,
        "query": "test",
    })
    assert resp.status_code == 200


async def test_standalone_wrong_tenant_forbidden(client):
    """Accessing a different tenant_id than the standalone tenant → 403."""
    _get_standalone_tenant()
    resp = await client.get("/api/v1/memories?tenant_id=nonexistent-tenant")
    assert resp.status_code == 403


async def test_standalone_entities_no_auth(client):
    """Entity listing works without auth in standalone mode."""
    tenant_id = _get_standalone_tenant()
    resp = await client.get(f"/api/v1/entities?tenant_id={tenant_id}")
    assert resp.status_code == 200


async def test_standalone_agents_no_auth(client):
    """Agent listing works without auth in standalone mode."""
    tenant_id = _get_standalone_tenant()
    resp = await client.get(f"/api/v1/agents?tenant_id={tenant_id}")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# tenant_id auto-injection (middleware injects when missing)
# ---------------------------------------------------------------------------


async def test_standalone_list_no_tenant_id(client):
    """List memories without tenant_id param — middleware injects it."""
    _get_standalone_tenant()
    resp = await client.get("/api/v1/memories")
    assert resp.status_code == 200


async def test_standalone_write_no_tenant_id(client):
    """Write a memory without tenant_id in body — middleware injects it."""
    _get_standalone_tenant()
    tag = _uid()
    resp = await client.post("/api/v1/memories", json={
        "content": f"No tenant_id in body test [{tag}]",
        "agent_id": f"inject-agent-{tag}",
        "fleet_id": f"inject-fleet-{tag}",
    })
    assert resp.status_code == 201


async def test_standalone_search_no_tenant_id(client):
    """Search without tenant_id in body — middleware injects it."""
    _get_standalone_tenant()
    resp = await client.post("/api/v1/search", json={
        "query": "test",
    })
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Integration: full standalone workflow (write → search → read → delete)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestStandaloneWorkflow:
    """End-to-end standalone workflow without any auth headers."""

    async def test_write_search_read_delete(self, client):
        """Full lifecycle: write memories, search them, read back, delete."""
        tenant_id = _get_standalone_tenant()
        tag = _uid()

        # Write two memories (use unique agent/fleet to avoid trust-level conflicts)
        m1 = await client.post("/api/v1/memories", json={
            "tenant_id": tenant_id,
            "content": f"The project uses PostgreSQL with pgvector for embeddings [{tag}]",
            "agent_id": f"integ-agent-{tag}",
            "fleet_id": f"integ-fleet-{tag}",
            "memory_type": "fact",
        })
        assert m1.status_code == 201
        m1_id = m1.json()["id"]

        m2 = await client.post("/api/v1/memories", json={
            "tenant_id": tenant_id,
            "content": f"Redis is used as a caching layer [{tag}]",
            "agent_id": f"integ-agent-{tag}",
            "fleet_id": f"integ-fleet-{tag}",
            "memory_type": "fact",
        })
        assert m2.status_code == 201
        m2_id = m2.json()["id"]

        # Search for one of them (use exact content substring for reliability with fake embeddings)
        search = await client.post("/api/v1/search", json={
            "tenant_id": tenant_id,
            "query": f"PostgreSQL with pgvector for embeddings [{tag}]",
        })
        assert search.status_code == 200
        results = search.json()
        assert len(results) >= 1

        # Read back by ID
        get_resp = await client.get(f"/api/v1/memories/{m1_id}?tenant_id={tenant_id}")
        assert get_resp.status_code == 200
        assert "PostgreSQL" in get_resp.json()["content"]

        # List all memories
        list_resp = await client.get(f"/api/v1/memories?tenant_id={tenant_id}")
        assert list_resp.status_code == 200
        items = list_resp.json()["items"]
        ids = [m["id"] for m in items]
        assert m1_id in ids
        assert m2_id in ids

        # Delete one
        del_resp = await client.delete(f"/api/v1/memories/{m2_id}?tenant_id={tenant_id}")
        assert del_resp.status_code == 204

        # Verify it's gone
        list_after = await client.get(f"/api/v1/memories?tenant_id={tenant_id}")
        ids_after = [m["id"] for m in list_after.json()["items"]]
        assert m2_id not in ids_after

    async def test_bulk_write(self, client):
        """Bulk write multiple memories without auth."""
        tenant_id = _get_standalone_tenant()
        tag = _uid()

        resp = await client.post(
            "/api/v1/memories/bulk",
            json={
                "tenant_id": tenant_id,
                "agent_id": f"bulk-agent-{tag}",
                "items": [
                    {"content": f"Bulk item 1: user prefers dark mode [{tag}]"},
                    {"content": f"Bulk item 2: user timezone is UTC+2 [{tag}]"},
                    {"content": f"Bulk item 3: user speaks Hebrew and English [{tag}]"},
                ],
            },
            headers={"X-Bulk-Attempt-Id": f"standalone-bulk-{tag}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] == 3

    async def test_agents_and_entities_populated(self, client):
        """After writing memories, agents and entities endpoints return data."""
        tenant_id = _get_standalone_tenant()
        tag = _uid()

        # Write a memory with a named entity
        await client.post("/api/v1/memories", json={
            "tenant_id": tenant_id,
            "content": f"John prefers to communicate via Slack [{tag}]",
            "agent_id": f"assistant-agent-{tag}",
            "fleet_id": f"comms-fleet-{tag}",
            "memory_type": "preference",
        })

        # Agents endpoint should list the agent
        agents = await client.get(f"/api/v1/agents?tenant_id={tenant_id}")
        assert agents.status_code == 200
        agent_ids = [a["agent_id"] for a in agents.json()]
        assert f"assistant-agent-{tag}" in agent_ids

    async def test_standalone_tenant_isolation(self, client):
        """Standalone mode enforces tenant isolation — can't access other tenants."""
        tenant_id = _get_standalone_tenant()
        tag = _uid()

        # Write to the standalone tenant works
        resp = await client.post("/api/v1/memories", json={
            "tenant_id": tenant_id,
            "content": f"Allowed write for isolation test [{tag}]",
            "agent_id": f"isolation-agent-{tag}",
            "fleet_id": f"isolation-fleet-{tag}",
        })
        assert resp.status_code == 201

        # Write to a different tenant is forbidden
        resp = await client.post("/api/v1/memories", json={
            "tenant_id": "some-other-tenant",
            "content": "Should be blocked",
            "agent_id": f"isolation-agent-{tag}",
            "fleet_id": f"isolation-fleet-{tag}",
        })
        assert resp.status_code == 403

    async def test_admin_endpoints_still_require_admin_key(self, client):
        """Admin endpoints remain protected even in standalone mode."""
        _get_standalone_tenant()
        resp = await client.get("/api/v1/admin/tenants")
        # Should be 401 or 403, not 200
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Integration: zero-config workflow (no auth, no tenant_id)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestZeroConfigWorkflow:
    """End-to-end workflow without auth headers or tenant_id — fully zero-config."""

    async def test_write_search_read_delete_no_tenant_id(self, client):
        """Full lifecycle without ever passing tenant_id."""
        # Ensure standalone is initialised
        _get_standalone_tenant()
        tag = _uid()

        # Write
        m1 = await client.post("/api/v1/memories", json={
            "content": f"Zero-config: the deployment uses Kubernetes [{tag}]",
            "agent_id": f"zero-agent-{tag}",
            "fleet_id": f"zero-fleet-{tag}",
            "memory_type": "fact",
        })
        assert m1.status_code == 201
        m1_id = m1.json()["id"]

        m2 = await client.post("/api/v1/memories", json={
            "content": f"Zero-config: CI pipeline runs on GitHub Actions [{tag}]",
            "agent_id": f"zero-agent-{tag}",
            "fleet_id": f"zero-fleet-{tag}",
            "memory_type": "fact",
        })
        assert m2.status_code == 201
        m2_id = m2.json()["id"]

        # Search (no tenant_id in body — use exact content for reliability with fake embeddings)
        search = await client.post("/api/v1/search", json={
            "query": f"deployment uses Kubernetes [{tag}]",
        })
        assert search.status_code == 200

        # Read by ID (no tenant_id in query)
        get_resp = await client.get(f"/api/v1/memories/{m1_id}")
        assert get_resp.status_code == 200
        assert "Kubernetes" in get_resp.json()["content"]

        # List (no tenant_id in query)
        list_resp = await client.get("/api/v1/memories")
        assert list_resp.status_code == 200
        ids = [m["id"] for m in list_resp.json()["items"]]
        assert m1_id in ids
        assert m2_id in ids

        # Delete (no tenant_id in query)
        del_resp = await client.delete(f"/api/v1/memories/{m2_id}")
        assert del_resp.status_code == 204

        # Verify deletion
        list_after = await client.get("/api/v1/memories")
        ids_after = [m["id"] for m in list_after.json()["items"]]
        assert m2_id not in ids_after

    async def test_bulk_write_no_tenant_id(self, client):
        """Bulk write without tenant_id in body."""
        _get_standalone_tenant()
        tag = _uid()

        resp = await client.post(
            "/api/v1/memories/bulk",
            json={
                "agent_id": f"zero-bulk-agent-{tag}",
                "items": [
                    {"content": f"Zero-config bulk item 1 [{tag}]"},
                    {"content": f"Zero-config bulk item 2 [{tag}]"},
                ],
            },
            headers={"X-Bulk-Attempt-Id": f"zero-bulk-{tag}"},
        )
        assert resp.status_code == 200
        assert resp.json()["created"] == 2

    async def test_entities_agents_no_tenant_id(self, client):
        """Entities and agents endpoints work without tenant_id."""
        _get_standalone_tenant()

        entities = await client.get("/api/v1/entities")
        assert entities.status_code == 200

        agents = await client.get("/api/v1/agents")
        assert agents.status_code == 200


# ---------------------------------------------------------------------------
# Standalone mode with explicit auth
# ---------------------------------------------------------------------------


async def test_standalone_explicit_auth_still_works(client):
    """Providing an API key in standalone mode uses normal auth flow."""
    tenant_id = _get_standalone_tenant()
    headers = {"X-API-Key": "test-admin-key"}
    resp = await client.get(
        f"/api/v1/memories?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp.status_code == 200


async def test_non_standalone_requires_auth(client, monkeypatch):
    """When is_standalone=False, unauthenticated requests are rejected."""
    from core_api.config import settings
    monkeypatch.setattr(settings, "is_standalone", False)
    resp = await client.get("/api/v1/memories?tenant_id=any-tenant")
    # Without admin_key configured: AuthContext(tenant_id=None) → enforce_tenant → 403
    # With admin_key configured: missing API key → 401
    assert resp.status_code in (401, 403)
