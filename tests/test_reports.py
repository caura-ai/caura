"""Integration tests for GET /api/v1/reports — the governed two-check read.

Real FastAPI app + in-process storage (see conftest). Validates:
- durable corpus filter (excludes the ``episode`` type and the ``main`` firehose),
- destination narrowing (owner_1to1 = self, internal_group = fleet, external = fail-closed),
- period validation.
"""

import pytest

from core_api.app import app
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from tests.conftest import get_test_auth, uid as _uid

pytestmark = pytest.mark.asyncio


async def _register(tenant_id, fleet, *agents):
    sc = get_storage_client()
    for a in agents:
        await sc.create_or_update_agent(
            {"tenant_id": tenant_id, "agent_id": a, "fleet_id": fleet, "trust_level": 1}
        )


async def _seed(client, headers, tenant_id, fleet, agent, mtype, n=1):
    for i in range(n):
        r = await client.post(
            "/api/v1/memories",
            json={
                "tenant_id": tenant_id,
                "agent_id": agent,
                "fleet_id": fleet,
                "memory_type": mtype,
                "visibility": "scope_team",
                "content": f"{mtype} by {agent} #{i} {_uid()}",
            },
            headers=headers,
        )
        assert r.status_code == 201, r.text


async def test_report_internal_group_durable_filter(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet, a1, a2 = f"rep-fleet-{tag}", f"rep-a1-{tag}", f"rep-a2-{tag}"
    await _register(tenant_id, fleet, a1, a2)
    await _seed(client, headers, tenant_id, fleet, a1, "decision", 2)
    await _seed(client, headers, tenant_id, fleet, a2, "fact", 1)
    await _seed(
        client, headers, tenant_id, fleet, a1, "episode", 1
    )  # excluded: episodic
    await _seed(
        client, headers, tenant_id, fleet, "main", "fact", 1
    )  # excluded: firehose

    resp = await client.get(
        "/api/v1/reports",
        params={
            "tenant_id": tenant_id,
            "period": "week",
            "destination": "internal_group",
            "agent_id": a1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary"]["durable_memories_written"] == 3, body
    assert body["summary"]["active_agents"] == 2, body
    agents = {p["agent_id"]: p["durable_writes"] for p in body["per_agent"]}
    assert agents == {a1: 2, a2: 1}, agents
    assert "main" not in agents
    assert "episode" not in body["summary"]["by_type"], body["summary"]["by_type"]
    # weekly extras: value_highlights (top durable) + spotlight (top contributor)
    assert len(body["value_highlights"]) == 3, body["value_highlights"]
    assert all(
        "episode" != h["type"] and h["agent_id"] != "main"
        for h in body["value_highlights"]
    )
    assert body["spotlight"]["agent_id"] == a1, body["spotlight"]
    assert body["spotlight"]["durable_writes"] == 2
    assert body["spotlight"]["headline"] is not None
    # activity-over-time trend (14 daily buckets) + working-on lanes
    assert len(body["trend"]) == 14, body["trend"]
    assert sum(pt["count"] for pt in body["trend"]) == 3, body["trend"]
    assert set(body["working_on"]) == {"Governing", "Building", "Operating"}
    assert body["working_on"]["Governing"]["count"] == 2, body[
        "working_on"
    ]  # 2 decisions
    assert body["working_on"]["Building"]["count"] == 1, body["working_on"]  # 1 fact


async def test_report_owner_1to1_is_self(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet, a1, a2 = f"rep-fleet-{tag}", f"rep-a1-{tag}", f"rep-a2-{tag}"
    await _register(tenant_id, fleet, a1, a2)
    await _seed(client, headers, tenant_id, fleet, a1, "decision", 2)
    await _seed(client, headers, tenant_id, fleet, a2, "fact", 1)

    resp = await client.get(
        "/api/v1/reports",
        params={
            "tenant_id": tenant_id,
            "period": "week",
            "destination": "owner_1to1",
            "agent_id": a1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meta"]["scope"] == "self", body["meta"]
    assert body["summary"]["durable_memories_written"] == 2, body  # only a1's own


async def test_report_external_is_fail_closed(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet, a1 = f"rep-fleet-{tag}", f"rep-a1-{tag}"
    await _register(tenant_id, fleet, a1)
    await _seed(client, headers, tenant_id, fleet, a1, "decision", 2)

    resp = await client.get(
        "/api/v1/reports",
        params={
            "tenant_id": tenant_id,
            "period": "week",
            "destination": "external",
            "agent_id": a1,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meta"]["destination"] == "external"
    assert body["per_agent"] == [], body  # fail-closed: no per-agent detail
    assert body["learning"] == [], body


async def test_report_unknown_destination_and_invalid_period(client):
    tenant_id, headers = get_test_auth()
    a1 = f"rep-a1-{_uid()}"
    # Unknown destination → coerced to most-restrictive ``external`` (still 200).
    r1 = await client.get(
        "/api/v1/reports",
        params={
            "tenant_id": tenant_id,
            "period": "week",
            "destination": "bogus",
            "agent_id": a1,
        },
        headers=headers,
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["meta"]["destination"] == "external"
    # Invalid period → 422.
    r2 = await client.get(
        "/api/v1/reports",
        params={"tenant_id": tenant_id, "period": "month", "agent_id": a1},
        headers=headers,
    )
    assert r2.status_code == 422, r2.text


async def test_report_no_agent_caller_is_group_view(client):
    """Human/tenant caller (no agent_id) → tenant group view, NOT a 403.

    Regression guard for the auth fix: the trust gate only runs for agent
    callers; a tenant member/admin with no agent identity is authorized for the
    group view by enforce_tenant. Uses a dedicated tenant so the tenant-wide
    (no-fleet) group view is isolated from other tests.
    """
    tag = _uid()
    tenant_id, headers = get_test_auth(f"rep-tenant-{tag}")
    fleet, a1, a2 = f"rep-fleet-{tag}", f"rep-a1-{tag}", f"rep-a2-{tag}"
    await _register(tenant_id, fleet, a1, a2)
    await _seed(client, headers, tenant_id, fleet, a1, "decision", 2)
    await _seed(client, headers, tenant_id, fleet, a2, "fact", 1)
    await _seed(client, headers, tenant_id, fleet, "main", "fact", 2)  # excluded

    resp = await client.get(
        "/api/v1/reports",
        params={
            "tenant_id": tenant_id,
            "period": "week",
            "destination": "internal_group",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text  # the fix: not 403
    body = resp.json()
    assert body["meta"]["scope"] == "group"
    agents = {p["agent_id"]: p["durable_writes"] for p in body["per_agent"]}
    assert agents == {a1: 2, a2: 1}, agents
    assert body["summary"]["durable_memories_written"] == 3, body


async def test_report_org_scope_aggregates_across_tenants(client):
    """scope=org with a cross-tenant read credential aggregates across the
    readable tenant set and returns a per-tenant breakdown."""
    tag = _uid()
    t1, t2 = f"rep-org1-{tag}", f"rep-org2-{tag}"
    _, headers = get_test_auth(
        t1
    )  # admin key — used only to SEED (before the override)
    await _register(t1, f"f1-{tag}", f"a1-{tag}")
    await _register(t2, f"f2-{tag}", f"a2-{tag}")
    await _seed(client, headers, t1, f"f1-{tag}", f"a1-{tag}", "decision", 2)
    await _seed(client, headers, t2, f"f2-{tag}", f"a2-{tag}", "fact", 3)

    ctx = AuthContext(tenant_id=t1, readable_tenant_ids=[t1, t2])  # cross-tenant reader
    app.dependency_overrides[get_auth_context] = lambda: ctx
    try:
        resp = await client.get(
            "/api/v1/reports",
            params={
                "tenant_id": t1,
                "period": "week",
                "destination": "internal_group",
                "scope": "org",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["meta"]["scope"] == "org"
        assert body["summary"]["durable_memories_written"] == 5, body["summary"]
        assert body["summary"]["by_tenant"] == {t1: 2, t2: 3}, body["summary"][
            "by_tenant"
        ]
    finally:
        app.dependency_overrides.pop(get_auth_context, None)


async def test_report_org_scope_admin_readable_param(client):
    """The internal admin credential may pass an explicit ``readable_tenant_ids``
    (the org-report proxy path). A non-admin caller's value is ignored — asserted
    by the sibling override tests, which never pass the param.
    """
    tag = _uid()
    t1, t2 = f"rep-adm1-{tag}", f"rep-adm2-{tag}"
    _, headers = get_test_auth(t1)  # admin key (is_admin=True)
    await _register(t1, f"f1-{tag}", f"a1-{tag}")
    await _register(t2, f"f2-{tag}", f"a2-{tag}")
    await _seed(client, headers, t1, f"f1-{tag}", f"a1-{tag}", "decision", 2)
    await _seed(client, headers, t2, f"f2-{tag}", f"a2-{tag}", "fact", 3)

    resp = await client.get(
        "/api/v1/reports",
        params={
            "tenant_id": t1,
            "period": "week",
            "destination": "internal_group",
            "scope": "org",
            "readable_tenant_ids": f"{t1},{t2}",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meta"]["scope"] == "org"
    assert body["summary"]["durable_memories_written"] == 5, body["summary"]
    assert body["summary"]["by_tenant"] == {t1: 2, t2: 3}, body["summary"]["by_tenant"]


async def test_report_org_scope_requires_cross_tenant_key(client):
    """scope=org without a cross-tenant read credential → 403 (home-only key can't widen)."""
    tag = _uid()
    t1 = f"rep-org-solo-{tag}"
    ctx = AuthContext(tenant_id=t1)  # single-tenant, non-admin
    app.dependency_overrides[get_auth_context] = lambda: ctx
    try:
        resp = await client.get(
            "/api/v1/reports",
            params={"tenant_id": t1, "period": "week", "scope": "org"},
        )
        assert resp.status_code == 403, resp.text
    finally:
        app.dependency_overrides.pop(get_auth_context, None)
