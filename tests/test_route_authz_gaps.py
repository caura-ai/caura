"""Route-level authorization gaps surfaced by the 2026-06-11 audit.

- ``POST /fleet/commands/{id}/result`` had no tenant enforcement: the
  storage UPDATE keyed only on ``command_id``, so any authenticated tenant
  could mark another tenant's command done/failed by UUID (cross-tenant
  BOLA). The UPDATE is now tenant-scoped and the route 404s on mismatch.
- ``POST /memories/redistribute`` ran its trust_level >= 3 gate against
  the caller-controlled ``agent_id`` query param instead of the
  authenticated identity — a low-trust agent credential could clear the
  gate by naming a trust-3 agent (privilege escalation).
- STM write endpoints (``DELETE /stm/notes``, ``DELETE /stm/bulletin``,
  ``POST /stm/promote``) skipped ``enforce_read_only`` /
  ``enforce_usage_limits`` and accepted a caller-controlled agent_id.
- ``DELETE /memories/{id}`` audit-logged the raw ``agent_id`` query param
  instead of the effective (gateway-verified) identity.

NOTE: requests in these tests pass explicit ``tenant_id`` in JSON bodies
where applicable — ``StandaloneTenantMiddleware`` otherwise injects the
standalone tenant into body/query, which would mask the cross-tenant
scenarios. Agent rows are seeded via the storage client (``sc``), not the
rolled-back ``db`` fixture, so the in-process storage app can see them.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def as_auth(monkeypatch):
    """Override get_auth_context with a controlled AuthContext.

    Mirrors what the enterprise gateway header-trust path produces without
    needing a real gateway (standalone test mode otherwise pins identity).
    """
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str, agent_id: str | None = None, **kwargs):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id,
                agent_id=agent_id,
                readable_tenant_ids=[tenant_id],
                **kwargs,
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    from core_api.app import app as _app
    from core_api.auth import get_auth_context as _gac

    _app.dependency_overrides.pop(_gac, None)


def _uid() -> str:
    return uuid.uuid4().hex[:8]


async def _make_command(client, as_auth, tenant_id: str) -> str:
    """Heartbeat a node and dispatch a command for ``tenant_id``; return command id."""
    as_auth(tenant_id)
    resp = await client.post(
        "/api/v1/fleet/heartbeat",
        json={
            "tenant_id": tenant_id,
            "node_name": f"node-{_uid()}",
            "fleet_id": f"fleet-{_uid()}",
        },
    )
    assert resp.status_code == 200, resp.text
    node_id = resp.json()["node_id"]

    resp = await client.post(
        "/api/v1/fleet/commands",
        json={
            "tenant_id": tenant_id,
            "node_id": node_id,
            "command": "ping",
            "payload": {},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _seed_agent(sc, tenant_id: str, agent_id: str, trust_level: int):
    await sc.create_or_update_agent(
        {"tenant_id": tenant_id, "agent_id": agent_id, "trust_level": trust_level}
    )


# ---------------------------------------------------------------------------
# S2 — command_result tenant enforcement
# ---------------------------------------------------------------------------


async def test_command_result_cross_tenant_is_404(client, as_auth):
    victim = f"victim-{_uid()}"
    attacker = f"attacker-{_uid()}"
    command_id = await _make_command(client, as_auth, victim)

    as_auth(attacker)
    resp = await client.post(
        f"/api/v1/fleet/commands/{command_id}/result",
        json={"status": "done", "result": {"injected": True}},
    )
    assert resp.status_code == 404

    # The victim's command must be untouched.
    as_auth(victim)
    resp = await client.get(f"/api/v1/fleet/commands?tenant_id={victim}")
    assert resp.status_code == 200
    cmd = next(c for c in resp.json() if c["id"] == command_id)
    assert cmd["status"] == "pending"
    assert cmd.get("result") in (None, {})


async def test_command_result_same_tenant_persists(client, as_auth):
    tenant = f"tenant-{_uid()}"
    command_id = await _make_command(client, as_auth, tenant)

    resp = await client.post(
        f"/api/v1/fleet/commands/{command_id}/result",
        json={"status": "done", "result": {"exit_code": 0}},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(f"/api/v1/fleet/commands?tenant_id={tenant}")
    cmd = next(c for c in resp.json() if c["id"] == command_id)
    assert cmd["status"] == "done"
    assert cmd["result"] == {"exit_code": 0}


# ---------------------------------------------------------------------------
# S3 — redistribute trust gate binds to the authenticated agent
# ---------------------------------------------------------------------------


async def test_redistribute_rejects_asserted_admin_identity(client, as_auth, sc):
    """A low-trust agent credential must not clear the trust gate by naming
    a trust-3 agent in the query string."""
    tenant = f"tenant-{_uid()}"
    await _seed_agent(sc, tenant, "admin-agent", 3)
    await _seed_agent(sc, tenant, "low-agent", 1)
    await _seed_agent(sc, tenant, "target-agent", 1)

    as_auth(tenant, agent_id="low-agent")
    resp = await client.post(
        f"/api/v1/memories/redistribute?tenant_id={tenant}&agent_id=admin-agent",
        json={"memory_ids": [str(uuid.uuid4())], "target_agent_id": "target-agent"},
    )
    assert resp.status_code == 403
    assert "does not match the authenticated agent identity" in resp.text


async def test_redistribute_allows_matching_admin_identity(client, as_auth, sc):
    tenant = f"tenant-{_uid()}"
    await _seed_agent(sc, tenant, "admin-agent", 3)
    await _seed_agent(sc, tenant, "target-agent", 1)

    as_auth(tenant, agent_id="admin-agent")
    resp = await client.post(
        f"/api/v1/memories/redistribute?tenant_id={tenant}&agent_id=admin-agent",
        json={"memory_ids": [str(uuid.uuid4())], "target_agent_id": "target-agent"},
    )
    assert resp.status_code == 200, resp.text


async def test_redistribute_user_credential_unchanged(client, as_auth, sc):
    """Dashboard/user credentials (no agent identity) keep the existing
    contract: the gate runs against the supplied agent_id."""
    tenant = f"tenant-{_uid()}"
    await _seed_agent(sc, tenant, "admin-agent", 3)
    await _seed_agent(sc, tenant, "target-agent", 1)

    as_auth(tenant, agent_id=None)
    resp = await client.post(
        f"/api/v1/memories/redistribute?tenant_id={tenant}&agent_id=admin-agent",
        json={"memory_ids": [str(uuid.uuid4())], "target_agent_id": "target-agent"},
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# S4 — STM write endpoints honor read-only / agent binding
# ---------------------------------------------------------------------------


@pytest.fixture
def _stm_enabled(monkeypatch):
    from core_api.config import settings

    monkeypatch.setattr(settings, "use_stm", True)


async def test_stm_clear_notes_blocked_for_read_only(client, as_auth, _stm_enabled):
    as_auth("tenant-ro", capabilities={"read"})
    resp = await client.delete("/api/v1/stm/notes?agent_id=any-agent")
    assert resp.status_code == 403


async def test_stm_clear_bulletin_blocked_for_read_only(client, as_auth, _stm_enabled):
    as_auth("tenant-ro", capabilities={"read"})
    resp = await client.delete("/api/v1/stm/bulletin?fleet_id=any-fleet")
    assert resp.status_code == 403


async def test_stm_promote_blocked_for_read_only(client, as_auth, _stm_enabled):
    as_auth("tenant-ro", capabilities={"read"})
    resp = await client.post(
        "/api/v1/stm/promote",
        json={"agent_id": "any-agent", "content": "should not persist"},
    )
    assert resp.status_code == 403


async def test_stm_clear_notes_rejects_peer_agent(client, as_auth, _stm_enabled):
    as_auth("tenant-a", agent_id="agent-1")
    resp = await client.delete("/api/v1/stm/notes?agent_id=agent-2")
    assert resp.status_code == 403


async def test_stm_promote_rejects_peer_agent(client, as_auth, _stm_enabled):
    as_auth("tenant-a", agent_id="agent-1")
    resp = await client.post(
        "/api/v1/stm/promote",
        json={"agent_id": "agent-2", "content": "on behalf of a peer"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# M5 — delete audit row attributes the effective identity
# ---------------------------------------------------------------------------


async def test_delete_audit_attributes_gateway_agent(client, as_auth, sc):
    """A gateway agent credential deleting WITHOUT the agent_id query param
    must be attributed to its verified identity, not None."""
    from sqlalchemy import select

    from common.models.audit import AuditLog
    from core_storage_api.services.postgres_service import get_read_session

    tenant = f"tenant-{_uid()}"
    await _seed_agent(sc, tenant, "deleter-agent", 3)

    as_auth(tenant, agent_id="deleter-agent")
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant,
            "agent_id": "deleter-agent",
            "memory_type": "fact",
            "content": f"to delete {_uid()}",
        },
    )
    assert resp.status_code == 201, resp.text
    memory_id = resp.json()["id"]

    resp = await client.delete(f"/api/v1/memories/{memory_id}?tenant_id={tenant}")
    assert resp.status_code == 204, resp.text

    async with get_read_session() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.tenant_id == tenant,
                        AuditLog.action == "delete",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows, "expected a delete audit row"
    assert rows[-1].agent_id == "deleter-agent"
