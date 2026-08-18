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


# ---------------------------------------------------------------------------
# H-12 / H-13 / H-15 — write-shaped routes missing their capability gates
#
# Surfaced by the 2026-08-14 OSS/platform audit. All three are the same shape as
# the 2026-06-11 findings above: a mutating route that skipped a gate its own
# neighbours already applied.
#
#   H-13  POST /fleet/commands       — no enforce_tenant AND no enforce_read_only,
#                                      with the target tenant taken from the BODY
#   H-12  PATCH /agents/{id}/trust   — no enforce_read_only
#   H-15  PUT  /settings             — checked is_demo by hand, so it caught the
#                                      demo sandbox but not a read-only credential
# ---------------------------------------------------------------------------

READ_ONLY = {"read"}


async def test_fleet_command_cannot_be_queued_into_another_tenant(client, as_auth):
    """H-13: the queued command's tenant came from ``body.tenant_id``, unchecked.

    The GET sibling has always called ``enforce_tenant``, so the write was the
    weaker half of the pair.
    """
    victim = f"victim-{_uid()}"
    attacker = f"attacker-{_uid()}"

    # A real node in the victim's fleet, created by the victim.
    as_auth(victim)
    resp = await client.post(
        "/api/v1/fleet/heartbeat",
        json={
            "tenant_id": victim,
            "node_name": f"node-{_uid()}",
            "fleet_id": f"fleet-{_uid()}",
        },
    )
    assert resp.status_code == 200, resp.text
    node_id = resp.json()["node_id"]

    as_auth(attacker)
    resp = await client.post(
        "/api/v1/fleet/commands",
        json={
            "tenant_id": victim,
            "node_id": node_id,
            "command": "ping",
            "payload": {"injected": True},
        },
    )
    assert resp.status_code == 403, resp.text

    # And nothing landed in the victim's queue.
    as_auth(victim)
    resp = await client.get(f"/api/v1/fleet/commands?tenant_id={victim}")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_fleet_command_rejects_a_read_only_credential(client, as_auth):
    """H-13, second half: queueing a command is a write."""
    tenant = f"tenant-{_uid()}"

    as_auth(tenant)
    resp = await client.post(
        "/api/v1/fleet/heartbeat",
        json={
            "tenant_id": tenant,
            "node_name": f"node-{_uid()}",
            "fleet_id": f"fleet-{_uid()}",
        },
    )
    node_id = resp.json()["node_id"]

    as_auth(tenant, capabilities=READ_ONLY)
    resp = await client.post(
        "/api/v1/fleet/commands",
        json={
            "tenant_id": tenant,
            "node_id": node_id,
            "command": "ping",
            "payload": {},
        },
    )
    assert resp.status_code == 403, resp.text


async def test_agent_trust_rejects_a_read_only_credential(client, as_auth, sc):
    """H-12: trust is the master key to the ladder — a read key must not move it."""
    tenant = f"tenant-{_uid()}"
    agent = f"agent-{_uid()}"
    await _seed_agent(sc, tenant, agent, trust_level=1)

    as_auth(tenant, capabilities=READ_ONLY)
    resp = await client.patch(
        f"/api/v1/agents/{agent}/trust?tenant_id={tenant}",
        json={"trust_level": 3},
    )
    assert resp.status_code == 403, resp.text

    # The ladder did not move.
    as_auth(tenant)
    resp = await client.get(f"/api/v1/agents?tenant_id={tenant}")
    assert resp.status_code == 200
    row = next(a for a in resp.json() if a["agent_id"] == agent)
    assert row["trust_level"] == 1


async def test_agent_trust_still_works_when_over_usage_limits(client, as_auth, sc):
    """Pins a deliberate omission, so nobody "fixes" it by adding the gate.

    ``enforce_usage_limits`` is NOT applied to this route, unlike the
    neighbouring fleet-reassignment one. This is the route you reach for to
    DEMOTE a misbehaving agent, and an over-quota tenant must still be able to
    take trust away — quota state must not stand between an operator and a
    mitigation.
    """
    tenant = f"tenant-{_uid()}"
    agent = f"agent-{_uid()}"
    await _seed_agent(sc, tenant, agent, trust_level=3)

    as_auth(tenant, is_read_only=True)
    resp = await client.patch(
        f"/api/v1/agents/{agent}/trust?tenant_id={tenant}",
        json={"trust_level": 0},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["trust_level"] == 0


async def test_settings_rejects_a_read_only_credential(client, as_auth):
    """H-15: the hand-rolled ``is_demo`` check missed read-only credentials.

    Tenant settings carry security-relevant toggles — ``require_agent_approval``
    governs whether new agents start quarantined — so a viewer/reporting key
    rewriting them is a privilege escalation, not a cosmetic gap.
    """
    tenant = f"tenant-{_uid()}"
    as_auth(tenant, capabilities=READ_ONLY)
    resp = await client.put(
        "/api/v1/settings",
        json={"tenant_id": tenant, "require_agent_approval": False},
    )
    assert resp.status_code == 403, resp.text


async def test_settings_still_refuses_the_demo_sandbox(client, as_auth):
    """Regression guard: the hand-rolled demo branch was REPLACED, not dropped.

    ``enforce_read_only`` covers demo and read-only capabilities together, but
    that only holds if it really does still refuse demo.
    """
    tenant = f"tenant-{_uid()}"
    as_auth(tenant, is_demo=True)
    resp = await client.put(
        "/api/v1/settings",
        json={"tenant_id": tenant, "require_agent_approval": False},
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# H-14 / H-16 — client-trusted identity on READ paths
#
# The 2026-06-11 pass fixed the identity precedence on WRITE paths (delete/update
# memory, DELETE /stm/notes, POST /stm/promote) and on /memories/redistribute's
# trust gate. It left two reads trusting a caller-supplied agent id:
#
#   H-16  GET  /stm/notes  — reads a peer's per-agent PRIVATE notes by naming it
#   H-14  POST /search     — filter_agent_id became the visibility identity AND
#                            the subject of the trust<2 fleet forcing
# ---------------------------------------------------------------------------


async def test_stm_notes_of_a_peer_agent_cannot_be_read(client, as_auth, _stm_enabled):
    """H-16: the DELETE twin has enforced this since June; the read had not.

    So a peer's notes could not be cleared, only read — disclosure was the half
    left open.
    """
    tenant = f"tenant-{_uid()}"
    as_auth(tenant, agent_id="agent-a")
    resp = await client.get("/api/v1/stm/notes?agent_id=agent-b")
    assert resp.status_code == 403, resp.text


async def test_stm_notes_of_own_agent_are_still_readable(client, as_auth, _stm_enabled):
    """The guard must not break an agent reading its OWN notes."""
    tenant = f"tenant-{_uid()}"
    as_auth(tenant, agent_id="agent-a")
    resp = await client.get("/api/v1/stm/notes?agent_id=agent-a")
    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_id"] == "agent-a"


async def test_search_cannot_borrow_a_peer_identity_via_filter_agent_id(
    client, as_auth
):
    """H-14: ``filter_agent_id`` fed the visibility identity AND the trust gate.

    Naming a peer both exposed that peer's scope_agent rows (with content, so a
    direct disclosure) and skipped the trust<2 fleet forcing when the named peer
    was trust>=2 — the same escalation /memories/redistribute was fixed for.
    """
    tenant = f"tenant-{_uid()}"
    as_auth(tenant, agent_id="agent-a")
    resp = await client.post(
        "/api/v1/search",
        json={
            "tenant_id": tenant,
            "query": "anything",
            "top_k": 5,
            "filter_agent_id": "agent-b",
        },
    )
    assert resp.status_code == 403, resp.text


async def test_search_filtering_to_own_agent_id_is_allowed(client, as_auth):
    tenant = f"tenant-{_uid()}"
    as_auth(tenant, agent_id="agent-a")
    resp = await client.post(
        "/api/v1/search",
        json={
            "tenant_id": tenant,
            "query": "anything",
            "top_k": 5,
            "filter_agent_id": "agent-a",
        },
    )
    assert resp.status_code == 200, resp.text


async def test_a_tenant_credential_may_still_filter_search_by_any_agent(
    client, as_auth
):
    """Pins the preserved case: the restriction targets AGENT-scoped credentials.

    A tenant/user credential (``auth.agent_id`` is None) is what the dashboard
    uses to inspect a given agent's memories, and must keep working.
    """
    tenant = f"tenant-{_uid()}"
    as_auth(tenant)  # no agent_id
    resp = await client.post(
        "/api/v1/search",
        json={
            "tenant_id": tenant,
            "query": "anything",
            "top_k": 5,
            "filter_agent_id": "agent-b",
        },
    )
    assert resp.status_code == 200, resp.text
