"""Test audit logging via real API calls.

All tests use httpx AsyncClient against the real FastAPI app with PostgreSQL.
"""

import pytest

from tests.conftest import get_test_auth
from tests.conftest import uid as _uid

pytestmark = pytest.mark.asyncio


# ── 1. Memory write creates audit entry ──


async def test_memory_write_creates_audit_entry(client):
    """Write a memory, GET /api/audit-log → has entry with action containing 'create' and resource_type='memory'."""
    tenant_id, headers = get_test_auth()
    tag = _uid()

    write_resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": f"audit-agent-{tag}",
            "fleet_id": f"audit-fleet-{tag}",
            "memory_type": "fact",
            "content": f"Audit test: single write entry [{tag}]",
        },
        headers=headers,
    )
    assert write_resp.status_code == 201, write_resp.text

    audit_resp = await client.get(
        "/api/v1/audit-log",
        params={
            "tenant_id": tenant_id,
        },
        headers=headers,
    )
    assert audit_resp.status_code == 200, audit_resp.text
    entries = audit_resp.json()
    assert len(entries) >= 1, "Audit log should have at least one entry after a write"

    # Find the create/write entry
    create_entries = [
        e for e in entries if "create" in e["action"] and e["resource_type"] == "memory"
    ]
    assert len(create_entries) >= 1, (
        f"Expected a 'create' audit entry for memory, got actions: {[e['action'] for e in entries]}"
    )


# ── 2. Multiple writes → multiple entries ──


async def test_multiple_writes_multiple_entries(client):
    """Write 3 memories, GET /api/audit-log → at least 3 entries."""
    tenant_id, headers = get_test_auth()
    tag = _uid()

    for i in range(3):
        resp = await client.post(
            "/api/v1/memories",
            json={
                "tenant_id": tenant_id,
                "agent_id": f"audit-agent-{tag}",
                "fleet_id": f"audit-fleet-{tag}",
                "memory_type": "fact",
                "content": f"Audit test memory number {i + 1} [{tag}]",
            },
            headers=headers,
        )
        assert resp.status_code == 201, resp.text

    audit_resp = await client.get(
        "/api/v1/audit-log",
        params={
            "tenant_id": tenant_id,
        },
        headers=headers,
    )
    assert audit_resp.status_code == 200, audit_resp.text
    entries = audit_resp.json()
    create_entries = [
        e for e in entries if "create" in e["action"] and e["resource_type"] == "memory"
    ]
    assert len(create_entries) >= 3, (
        f"Expected at least 3 create entries, got {len(create_entries)}"
    )


# ── 3. Audit contains agent_id ──


async def test_audit_contains_agent_id(client):
    """Write memory with agent_id, audit entry has the same agent_id."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent_id = f"my-agent-{tag}"

    write_resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "fleet_id": f"audit-fleet-{tag}",
            "memory_type": "fact",
            "content": f"Agent attribution audit test [{tag}]",
        },
        headers=headers,
    )
    assert write_resp.status_code == 201, write_resp.text

    audit_resp = await client.get(
        "/api/v1/audit-log",
        params={
            "tenant_id": tenant_id,
        },
        headers=headers,
    )
    entries = audit_resp.json()
    create_entries = [
        e for e in entries if "create" in e["action"] and e["resource_type"] == "memory"
    ]
    assert len(create_entries) >= 1
    matching = [e for e in create_entries if e.get("agent_id") == agent_id]
    assert len(matching) >= 1, (
        f"Expected agent_id='{agent_id}' in audit, got agents: {[e.get('agent_id') for e in create_entries]}"
    )


# ── 4. Filters and the keyset cursor (governance build plan row p1.40) ──


async def _write(
    client, tenant_id: str, headers: dict, agent_id: str, tag: str, n: int
) -> list[str]:
    ids = []
    for i in range(n):
        resp = await client.post(
            "/api/v1/memories",
            json={
                "tenant_id": tenant_id,
                "agent_id": agent_id,
                "fleet_id": f"audit-fleet-{tag}",
                "memory_type": "fact",
                "content": f"Audit paging test {i} [{tag}]",
            },
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["id"])
    return ids


async def test_agent_and_resource_filters_narrow_the_log(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    mine, other = f"filter-agent-{tag}", f"filter-other-{tag}"
    (memory_id,) = await _write(client, tenant_id, headers, mine, tag, 1)
    await _write(client, tenant_id, headers, other, tag, 2)

    async def _get(**filters):
        params = {"tenant_id": tenant_id, **filters}
        resp = await client.get("/api/v1/audit-log", params=params, headers=headers)
        assert resp.status_code == 200, resp.text
        return resp.json()

    by_agent = await _get(agent_id=mine)
    assert by_agent and {e["agent_id"] for e in by_agent} == {mine}
    by_memory = await _get(resource_id=memory_id)
    assert by_memory and {e["resource_id"] for e in by_memory} == {memory_id}
    for action in {e["action"] for e in by_agent}:
        narrowed = await _get(agent_id=mine, action=action)
        assert {e["action"] for e in narrowed} == {action}


async def test_the_cursor_pages_through_the_route_without_gaps(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent = f"page-agent-{tag}"
    await _write(client, tenant_id, headers, agent, tag, 4)

    scope = {"tenant_id": tenant_id, "agent_id": agent}
    resp = await client.get("/api/v1/audit-log", params=scope, headers=headers)
    everything = [e["id"] for e in resp.json()]
    assert len(everything) >= 4

    walked, cursor, pages = [], None, 0
    while True:
        params = {**scope, "limit": 2}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get("/api/v1/audit-log", params=params, headers=headers)
        assert resp.status_code == 200, resp.text
        walked.extend(e["id"] for e in resp.json())
        pages += 1
        cursor = resp.headers.get("X-Next-Cursor")
        if not cursor:
            break
        assert pages < 50, "the cursor is not advancing"
    assert walked == everything, "the cursor walk differs from the single page"
    assert pages == -(-len(everything) // 2), "the last page carried a next cursor"

    # A page holding exactly what is left carries no cursor either: the probe
    # row is what tells a full last page from a full middle one.
    params = {**scope, "limit": len(everything)}
    resp = await client.get("/api/v1/audit-log", params=params, headers=headers)
    assert [e["id"] for e in resp.json()] == everything
    assert "X-Next-Cursor" not in resp.headers, "a full last page carried a cursor"


async def test_a_malformed_cursor_is_refused(client):
    tenant_id, headers = get_test_auth()
    resp = await client.get(
        "/api/v1/audit-log",
        params={"tenant_id": tenant_id, "cursor": "not-a-cursor"},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text


# ── 5. resource_type filter and the chain seq (M-27) ──


async def _audit_rows(sc, tenant_id: str, agent_id: str) -> None:
    """One agent's three audit rows, of two resource types."""
    for resource_type, action in (
        ("memory", "create"),
        ("keystone", "update"),
        ("memory", "delete"),
    ):
        await sc.create_audit_log(
            {
                "tenant_id": tenant_id,
                "agent_id": agent_id,
                "action": action,
                "resource_type": resource_type,
            }
        )


async def test_the_audit_log_filters_by_resource_type(client, sc):
    """Storage filters by resource_type in SQL; the route did not expose it, so
    a caller could not ask for one kind of resource."""
    tenant_id, headers = get_test_auth()
    agent = f"m27-filter-{_uid()}"
    await _audit_rows(sc, tenant_id, agent)

    async def _types(resource_type: str) -> list[str]:
        resp = await client.get(
            "/api/v1/audit-log",
            params={
                "tenant_id": tenant_id,
                "agent_id": agent,
                "resource_type": resource_type,
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        return [e["resource_type"] for e in resp.json()]

    assert await _types("keystone") == ["keystone"]
    assert await _types("memory") == ["memory", "memory"]


async def test_each_audit_entry_carries_its_chain_seq(client, sc):
    """The /verify break report names rows by seq; an entry without its seq
    cannot be matched to the hash chain."""
    tenant_id, headers = get_test_auth()
    agent = f"m27-seq-{_uid()}"
    await _audit_rows(sc, tenant_id, agent)
    stored = {
        str(r["id"]): r["seq"]
        for r in await sc.list_audit_logs(tenant_id, agent_id=agent)
    }
    assert len(stored) == 3 and all(isinstance(s, int) for s in stored.values())

    resp = await client.get(
        "/api/v1/audit-log",
        params={"tenant_id": tenant_id, "agent_id": agent},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert {e["id"]: e.get("seq") for e in resp.json()} == stored
