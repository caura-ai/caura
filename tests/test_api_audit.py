"""Test audit logging via real API calls.

All tests use httpx AsyncClient against the real FastAPI app with PostgreSQL.
"""

import pytest

from tests.conftest import get_test_auth, uid as _uid

pytestmark = pytest.mark.asyncio


# ── 1. Memory write creates audit entry ──


async def test_memory_write_creates_audit_entry(client):
    """Write a memory, GET /api/audit-log → has entry with action containing 'create' and resource_type='memory'."""
    tenant_id, headers = get_test_auth()
    tag = _uid()

    write_resp = await client.post("/api/v1/memories", json={
        "tenant_id": tenant_id,
        "agent_id": f"audit-agent-{tag}",
        "fleet_id": f"audit-fleet-{tag}",
        "memory_type": "fact",
        "content": f"Audit test: single write entry [{tag}]",
    }, headers=headers)
    assert write_resp.status_code == 201, write_resp.text

    audit_resp = await client.get("/api/v1/audit-log", params={
        "tenant_id": tenant_id,
    }, headers=headers)
    assert audit_resp.status_code == 200, audit_resp.text
    entries = audit_resp.json()
    assert len(entries) >= 1, "Audit log should have at least one entry after a write"

    # Find the create/write entry
    create_entries = [e for e in entries if "create" in e["action"] and e["resource_type"] == "memory"]
    assert len(create_entries) >= 1, f"Expected a 'create' audit entry for memory, got actions: {[e['action'] for e in entries]}"


# ── 2. Multiple writes → multiple entries ──


async def test_multiple_writes_multiple_entries(client):
    """Write 3 memories, GET /api/audit-log → at least 3 entries."""
    tenant_id, headers = get_test_auth()
    tag = _uid()

    for i in range(3):
        resp = await client.post("/api/v1/memories", json={
            "tenant_id": tenant_id,
            "agent_id": f"audit-agent-{tag}",
            "fleet_id": f"audit-fleet-{tag}",
            "memory_type": "fact",
            "content": f"Audit test memory number {i + 1} [{tag}]",
        }, headers=headers)
        assert resp.status_code == 201, resp.text

    audit_resp = await client.get("/api/v1/audit-log", params={
        "tenant_id": tenant_id,
    }, headers=headers)
    assert audit_resp.status_code == 200, audit_resp.text
    entries = audit_resp.json()
    create_entries = [e for e in entries if "create" in e["action"] and e["resource_type"] == "memory"]
    assert len(create_entries) >= 3, f"Expected at least 3 create entries, got {len(create_entries)}"


# ── 3. Audit contains agent_id ──


async def test_audit_contains_agent_id(client):
    """Write memory with agent_id, audit entry has the same agent_id."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent_id = f"my-agent-{tag}"

    write_resp = await client.post("/api/v1/memories", json={
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "fleet_id": f"audit-fleet-{tag}",
        "memory_type": "fact",
        "content": f"Agent attribution audit test [{tag}]",
    }, headers=headers)
    assert write_resp.status_code == 201, write_resp.text

    audit_resp = await client.get("/api/v1/audit-log", params={
        "tenant_id": tenant_id,
    }, headers=headers)
    entries = audit_resp.json()
    create_entries = [e for e in entries if "create" in e["action"] and e["resource_type"] == "memory"]
    assert len(create_entries) >= 1
    matching = [e for e in create_entries if e.get("agent_id") == agent_id]
    assert len(matching) >= 1, (
        f"Expected agent_id='{agent_id}' in audit, got agents: {[e.get('agent_id') for e in create_entries]}"
    )
