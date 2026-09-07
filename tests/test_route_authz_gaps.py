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
scenarios. The STM requests now pass it in the QUERY STRING for the same
reason: since the WT-4 fix those routes declare a ``tenant_id`` selector,
so the injected standalone tenant would be read as a caller-supplied
cross-tenant request against these fabricated ``as_auth`` tenants and
answered with 403 TENANT_MISMATCH — an artifact of the fixture, not of
the routes (in a real standalone deployment every auth path resolves to
the standalone tenant, so the injected value always matches). Agent rows
are seeded via the storage client (``sc``), not the rolled-back ``db``
fixture, so the in-process storage app can see them.
"""

from __future__ import annotations

import uuid
from typing import NamedTuple

import pytest

from core_api import errors

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


class _Node(NamedTuple):
    fleet_id: str
    node_name: str
    node_id: str


async def _seed_node(client, as_auth, tenant_id: str) -> _Node:
    """Register one node in a fresh fleet, and hand back all three identifiers.

    Arms a plain tenant credential to do it, so a caller under test that holds
    a narrower one (read-only, or agent-scoped) starts from a node it did not
    create itself.
    """
    fleet_id = f"fleet-{_uid()}"
    node_name = f"node-{_uid()}"
    as_auth(tenant_id)
    resp = await client.post(
        "/api/v1/fleet/heartbeat",
        json={"tenant_id": tenant_id, "node_name": node_name, "fleet_id": fleet_id},
    )
    assert resp.status_code == 200, resp.text
    return _Node(fleet_id, node_name, resp.json()["node_id"])


async def _make_command(client, as_auth, tenant_id: str) -> str:
    """Heartbeat a node and dispatch a command for ``tenant_id``; return command id."""
    node_id = (await _seed_node(client, as_auth, tenant_id)).node_id

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


async def _seed_agent(sc, tenant_id: str, agent_id: str, trust_level: int, **extra):
    """Create an agent row. ``extra`` sets any other ``Agent`` column directly.

    Storage inserts the dict as given (``agent_add`` is
    ``pg_insert(Agent).values(**data)``) and ``search_profile`` is a real
    column in ``AGENT_FIELDS`` — so a fixture needing a pre-existing profile
    does not have to write one through the route under test, where a bug in
    the route would surface as a setup failure inside a gate test.
    """
    await sc.create_or_update_agent(
        {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "trust_level": trust_level,
            **extra,
        }
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
    resp = await client.delete(
        "/api/v1/stm/notes?agent_id=any-agent&tenant_id=tenant-ro"
    )
    assert resp.status_code == 403


async def test_stm_clear_bulletin_blocked_for_read_only(client, as_auth, _stm_enabled):
    as_auth("tenant-ro", capabilities={"read"})
    resp = await client.delete(
        "/api/v1/stm/bulletin?fleet_id=any-fleet&tenant_id=tenant-ro"
    )
    assert resp.status_code == 403


async def test_stm_promote_blocked_for_read_only(client, as_auth, _stm_enabled):
    as_auth("tenant-ro", capabilities={"read"})
    resp = await client.post(
        "/api/v1/stm/promote?tenant_id=tenant-ro",
        json={"agent_id": "any-agent", "content": "should not persist"},
    )
    assert resp.status_code == 403


async def test_stm_clear_notes_rejects_peer_agent(client, as_auth, _stm_enabled):
    as_auth("tenant-a", agent_id="agent-1")
    resp = await client.delete("/api/v1/stm/notes?agent_id=agent-2&tenant_id=tenant-a")
    assert resp.status_code == 403


async def test_stm_promote_rejects_peer_agent(client, as_auth, _stm_enabled):
    as_auth("tenant-a", agent_id="agent-1")
    resp = await client.post(
        "/api/v1/stm/promote?tenant_id=tenant-a",
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

# The two credentials that may never write, whatever route they reach:
# ``enforce_read_only`` refuses exactly these and nothing else. Shared so the
# parametrized users of the pair cannot drift apart — they already had, one
# spelling the capability set as a literal.
NON_WRITING_CREDS = [
    pytest.param({"capabilities": READ_ONLY}, id="read-only-key"),
    pytest.param({"is_demo": True}, id="demo-sandbox"),
]


async def test_fleet_command_cannot_be_queued_into_another_tenant(client, as_auth):
    """H-13: the queued command's tenant came from ``body.tenant_id``, unchecked.

    The GET sibling has always called ``enforce_tenant``, so the write was the
    weaker half of the pair.
    """
    victim = f"victim-{_uid()}"
    attacker = f"attacker-{_uid()}"

    # A real node in the victim's fleet, created by the victim.
    node_id = (await _seed_node(client, as_auth, victim)).node_id

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


async def test_fleet_command_cannot_target_another_tenants_node(client, as_auth):
    """H-13, third half: the node has to belong to the tenant the command lands in.

    ``enforce_tenant`` only checks ``body.tenant_id``, so a caller naming its OWN
    tenant clears it — while still pointing ``body.node_id`` at somebody else's
    node. The insert satisfies the FK to ``fleet_nodes.id`` on its own, so
    nothing about the write itself objects.

    Two independent gates stop it, and this test covers the write half — the
    404 below. Delivery is gated separately and was closed later, by #1173:
    ``fleet_get_pending_commands`` filters the (node, tenant, status) triple
    rather than ``node_id`` alone, so a row written before either fix is not
    handed over either.

    Queueing into a node you do not own is a 404, not a 403 — the same
    non-disclosing answer ``POST /fleet/commands/{id}/result`` gives for a
    command UUID belonging to another tenant. A 403 would confirm the node
    exists, turning the route into an existence oracle for node UUIDs.
    """
    victim = f"victim-{_uid()}"
    attacker = f"attacker-{_uid()}"

    # A real node in the victim's fleet, created by the victim.
    _, victim_node_name, victim_node_id = await _seed_node(client, as_auth, victim)

    # The attacker queues into its own tenant — the tenant gate passes cleanly —
    # but aims the command at the victim's node.
    as_auth(attacker)
    resp = await client.post(
        "/api/v1/fleet/commands",
        json={
            "tenant_id": attacker,
            "node_id": victim_node_id,
            "command": "deploy",
            "payload": {"injected": True},
        },
    )
    assert resp.status_code == 404, resp.text

    # The whole point: the victim's node must not be handed the command when it
    # next checks in. The victim's own queue listing cannot settle that — a row
    # filed under the ATTACKER's tenant never appears in it — so this heartbeat
    # is the assertion that spans both gates at once.
    as_auth(victim)
    resp = await client.post(
        "/api/v1/fleet/heartbeat",
        json={
            "tenant_id": victim,
            "node_name": victim_node_name,
            "fleet_id": f"fleet-{_uid()}",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["commands"] == []


async def test_fleet_command_for_an_unknown_node_is_404_not_500(client, as_auth):
    """A node UUID that exists nowhere must get the same 404 as a foreign one.

    Unguarded, this reaches the DB and raises ``ForeignKeyViolationError`` from
    the ``fleet_commands_node_id_fkey`` constraint — an unhandled 500. Beyond
    being the wrong status, a 500-vs-201 split tells the caller whether a node
    UUID exists in *any* tenant.
    """
    tenant = f"tenant-{_uid()}"

    as_auth(tenant)
    resp = await client.post(
        "/api/v1/fleet/commands",
        json={
            "tenant_id": tenant,
            "node_id": str(uuid.uuid4()),
            "command": "ping",
            "payload": {},
        },
    )
    assert resp.status_code == 404, resp.text


async def test_fleet_command_rejects_a_read_only_credential(client, as_auth):
    """H-13, second half: queueing a command is a write."""
    tenant = f"tenant-{_uid()}"
    node_id = (await _seed_node(client, as_auth, tenant)).node_id

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


# ---------------------------------------------------------------------------
# PATCH /agents/{agent_id}/tune had neither write gate.
#
# H-12 again, on the sibling route that sweep did not reach. Of the four
# mutating routes in ``routes/agents.py`` it was the only one without
# ``enforce_read_only`` — trust, fleet reassignment and agent deletion all call
# it. ``enforce_tenant`` and the self-plane check it does have say WHOSE
# profile may be written, never whether this credential may write at all.
#
# The tuning knobs reached through MCP ``caura_tune`` are guarded by
# ``_check_write_scope``, which is exactly ``enforce_read_only``'s
# write-capability half — so the capability was required of an MCP caller and
# not of a REST one. The two are not equivalent gates: ``enforce_read_only``
# also refuses ``is_demo``, which the MCP path never checks.
#
# NOT ``enforce_not_agent_credential``: an agent tuning its OWN profile is
# documented product behaviour, and the over-refusal guard below pins it.
# ---------------------------------------------------------------------------


async def _tuned_top_k(client, as_auth, tenant: str, agent: str):
    """The agent's stored ``top_k``, read back with a credential that may read it.

    RE-ARMS a plain tenant credential, so calling this mid-test replaces
    whatever narrow credential the test had installed. Read back AFTER the
    call under test, never before it.
    """
    as_auth(tenant)
    resp = await client.get(f"/api/v1/agents/{agent}/tune?tenant_id={tenant}")
    assert resp.status_code == 200, resp.text
    return (resp.json().get("search_profile") or {}).get("top_k")


@pytest.mark.parametrize("cred", NON_WRITING_CREDS)
@pytest.mark.parametrize(
    "attempt",
    [
        pytest.param({"json": {"top_k": 19}}, id="merge"),
        pytest.param({"json": {}, "query": "&reset=true"}, id="reset"),
    ],
)
async def test_agent_tune_rejects_a_non_writing_credential(
    client, as_auth, sc, cred, attempt
):
    """Both halves of ``enforce_read_only``, against both write branches.

    ``reset=true`` is parametrized because it is a second, separate storage
    write (``reset_search_profile``). A gate added inside the merge branch
    alone would leave it reachable, and that mutant fails only here.

    The profile is seeded through storage rather than through the route, so a
    bug in the route cannot masquerade as a setup failure in a gate test.
    """
    tenant = f"tenant-{_uid()}"
    agent = f"agent-{_uid()}"
    await _seed_agent(sc, tenant, agent, trust_level=1, search_profile={"top_k": 7})

    as_auth(tenant, **cred)
    resp = await client.patch(
        f"/api/v1/agents/{agent}/tune?tenant_id={tenant}{attempt.get('query', '')}",
        json=attempt["json"],
    )
    assert resp.status_code == 403, resp.text

    # The refusal has to mean the write never happened, not that it was
    # reported as refused afterwards. This is also the only check that the
    # seed landed, so a mismatch is not necessarily a gate bypass.
    assert await _tuned_top_k(client, as_auth, tenant, agent) == 7, (
        "expected the seeded profile to be intact: either the gate ran after "
        "the storage call, or the fixture never seeded"
    )


async def test_a_write_capable_agent_can_still_tune_itself(client, as_auth, sc):
    """OVER-REFUSAL GUARD, and the one that matters.

    Self-tune is the documented behaviour behind MCP ``caura_tune``, and this
    route is what the plugin's own tool PATCHes. Adding a write gate must not
    take it away from an agent credential that carries 'write' — which is the
    shape the enterprise gateway mints. ``test_agent_tune_self_allowed_peer_blocked``
    reaches the same path with a legacy ``capabilities=None`` key, though it
    only asserts ``!= 403`` on a self-tune that 404s — a weaker check than the
    200-plus-read-back here.
    """
    tenant = f"tenant-{_uid()}"
    agent = f"agent-{_uid()}"
    await _seed_agent(sc, tenant, agent, trust_level=1)

    as_auth(tenant, agent_id=agent, capabilities={"read", "write"})
    resp = await client.patch(
        f"/api/v1/agents/{agent}/tune?tenant_id={tenant}", json={"top_k": 11}
    )
    assert resp.status_code == 200, resp.text
    assert await _tuned_top_k(client, as_auth, tenant, agent) == 11


@pytest.mark.parametrize(
    "attempt",
    [
        pytest.param({"json": {"top_k": 3}, "expect": 3}, id="merge"),
        pytest.param({"json": {}, "query": "&reset=true", "expect": None}, id="reset"),
    ],
)
async def test_agent_tune_still_works_when_over_usage_limits(
    client, as_auth, sc, attempt
):
    """Pins a deliberate omission, so nobody "fixes" it by adding the gate.

    ``enforce_usage_limits`` is NOT applied here. The principle is the one
    stated on ``WRITE_QUOTA_OPS`` in ``usage_service``: an update that rewrites
    a row rather than adding one does not grow the store, and this writes a
    single column on a row that must already exist. Lowering ``top_k`` is also
    how an over-quota tenant reduces retrieval cost, so gating it would put
    plan state between them and the knob that gets them back under.

    The omission ships whether or not this test exists — the omission IS the
    decision. What the test buys is legibility: it is the difference between a
    choice and the oversight it would otherwise be indistinguishable from,
    which is the distinction ``usage_service`` draws about the same gate on the
    memory update route. Deleting this test is the whole cost of reversing the
    call.
    """
    tenant = f"tenant-{_uid()}"
    agent = f"agent-{_uid()}"
    await _seed_agent(sc, tenant, agent, trust_level=1, search_profile={"top_k": 7})

    as_auth(tenant, is_read_only=True)
    resp = await client.patch(
        f"/api/v1/agents/{agent}/tune?tenant_id={tenant}{attempt.get('query', '')}",
        json=attempt["json"],
    )
    assert resp.status_code == 200, resp.text
    # Not merely un-refused — the write has to have landed.
    assert await _tuned_top_k(client, as_auth, tenant, agent) == attempt["expect"]


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
# PATCH /memories/{id} — the capability gate its own neighbours already apply
#
# Same shape as H-12/H-13/H-15 above: a mutating route that skipped a gate its
# neighbours apply. The 2026-08-14 pass swept /fleet/commands,
# /agents/{id}/trust and /settings; it did not reach this one.
#
# Every other mutating memory route calls ``enforce_read_only`` — POST
# /memories, POST /memories/bulk, DELETE /memories/{id}, POST
# /memories/bulk-delete, PATCH /memories/{id}/status, POST
# /memories/redistribute. The route that rewrites content, title, metadata and
# weight is the only one that does not, so a credential minted read-only could
# overwrite any memory in its tenant. ``enforce_tenant`` does not cover this:
# it checks tenant binding and the admin bypass, never capabilities.
# ---------------------------------------------------------------------------


async def _seed_memory(client, tenant: str) -> str:
    """Create one memory as a write-capable caller and return its id."""
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant,
            "memory_type": "fact",
            "content": f"original content {_uid()}",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_update_memory_rejects_a_read_only_credential(client, as_auth):
    """A read-only credential must not rewrite a memory's content.

    ``enforce_read_only`` is the gate for BOTH the demo sandbox and a
    credential minted without the ``write`` capability. Its own error text
    calls that "a property of the CREDENTIAL, not of the endpoint" — which
    only holds if every write-shaped endpoint asks.
    """
    tenant = f"tenant-{_uid()}"
    as_auth(tenant)
    memory_id = await _seed_memory(client, tenant)

    as_auth(tenant, capabilities=READ_ONLY)
    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant}",
        json={"content": "rewritten by a read-only key"},
    )
    assert resp.status_code == 403, resp.text


async def test_update_memory_refuses_the_demo_sandbox(client, as_auth):
    """The demo half of the same gate — the sandbox is read-only end to end."""
    tenant = f"tenant-{_uid()}"
    as_auth(tenant)
    memory_id = await _seed_memory(client, tenant)

    as_auth(tenant, is_demo=True)
    resp = await client.patch(
        f"/api/v1/memories/{memory_id}?tenant_id={tenant}",
        json={"content": "rewritten from the demo sandbox"},
    )
    assert resp.status_code == 403, resp.text


async def test_delete_memory_already_rejects_a_read_only_credential(client, as_auth):
    """The neighbour that has always had the gate.

    Kept alongside the two above so the asymmetry is visible in the suite
    rather than only in review: removing a memory was refused while rewriting
    its contents was not.
    """
    tenant = f"tenant-{_uid()}"
    as_auth(tenant)
    memory_id = await _seed_memory(client, tenant)

    as_auth(tenant, capabilities=READ_ONLY)
    resp = await client.delete(f"/api/v1/memories/{memory_id}?tenant_id={tenant}")
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
    resp = await client.get(f"/api/v1/stm/notes?agent_id=agent-b&tenant_id={tenant}")
    assert resp.status_code == 403, resp.text


async def test_stm_notes_of_own_agent_are_still_readable(client, as_auth, _stm_enabled):
    """The guard must not break an agent reading its OWN notes."""
    tenant = f"tenant-{_uid()}"
    as_auth(tenant, agent_id="agent-a")
    resp = await client.get(f"/api/v1/stm/notes?agent_id=agent-a&tenant_id={tenant}")
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


# ---------------------------------------------------------------------------
# H-17 — /stm/promote owed the LTM write gates
#
# The June 2026 pass gave promote enforce_read_only / enforce_usage_limits and
# bound its agent_id to the authenticated identity. It still reached LTM without
# the gates POST /memories applies, so the STM door into long-term memory was
# cheaper than the front door: reserved memory types, agent approval, the
# trust==0 quarantine gate, fleet-write policy and write metering were all
# skipped.
# ---------------------------------------------------------------------------


async def test_promote_rejects_a_server_reserved_memory_type(
    client, as_auth, sc, _stm_enabled
):
    """POST /memories has rejected these at the boundary; promote did not."""
    tenant = f"tenant-{_uid()}"
    await _seed_agent(sc, tenant, "agent-a", trust_level=2)

    as_auth(tenant, agent_id="agent-a")
    resp = await client.post(
        f"/api/v1/stm/promote?tenant_id={tenant}",
        json={
            "agent_id": "agent-a",
            "content": "promoted",
            "memory_type": "rule",
        },
    )
    assert resp.status_code == 422, resp.text
    assert "server-reserved" in resp.text


async def test_promote_refuses_a_quarantined_agent(client, as_auth, sc, _stm_enabled):
    """trust_level 0 is the quarantine state; it must hold at every LTM door."""
    tenant = f"tenant-{_uid()}"
    await _seed_agent(sc, tenant, "agent-q", trust_level=0)

    as_auth(tenant, agent_id="agent-q")
    resp = await client.post(
        f"/api/v1/stm/promote?tenant_id={tenant}",
        json={"agent_id": "agent-q", "content": "promoted"},
    )
    assert resp.status_code == 403, resp.text
    assert "not approved" in resp.text


# ---------------------------------------------------------------------------
# H-04 — POST /fleet/heartbeat had no enforce_read_only
#
# The heartbeat reads like telemetry but is the most write-heavy route in the
# fleet module, and it was the only mutating one in it without the gate. Its
# five siblings all have it.
# ---------------------------------------------------------------------------


async def _queue_command_for(client, as_auth, tenant: str) -> tuple[str, str]:
    """Register a node and queue one command for it. Returns (node_name, command_id)."""
    _, node_name, node_id = await _seed_node(client, as_auth, tenant)

    resp = await client.post(
        "/api/v1/fleet/commands",
        json={
            "tenant_id": tenant,
            "node_id": node_id,
            "command": "ping",
            "payload": {"k": "v"},
        },
    )
    assert resp.status_code == 201, resp.text
    return node_name, resp.json()["id"]


@pytest.mark.parametrize("cred", NON_WRITING_CREDS)
async def test_heartbeat_refuses_a_non_writing_credential(client, as_auth, cred):
    """A credential that cannot write must not be able to heartbeat.

    ``upsert_node`` replaces the whole node row — hostname, versions, the
    metadata blob — so a read-only or demo credential could rewrite another
    node's identity, or register nodes that do not exist.
    """
    tenant = f"tenant-{_uid()}"
    as_auth(tenant, **cred)
    resp = await client.post(
        "/api/v1/fleet/heartbeat",
        json={
            "tenant_id": tenant,
            "node_name": f"node-{_uid()}",
            "fleet_id": f"fleet-{_uid()}",
        },
    )
    assert resp.status_code == 403, resp.text


async def test_read_only_credential_cannot_drain_a_nodes_command_queue(client, as_auth):
    """THE ATTACK, and the reason this is not merely an unwanted write.

    The heartbeat response carries the node's pending commands and then
    ``ack_commands`` them. Acked commands are not redelivered, so a caller
    who names an existing node both RECEIVES payloads intended for it and
    leaves nothing for the real node to collect. One request, unrecoverable:
    the operator sees a command that was acknowledged and never ran.

    Asserts both halves — the payload must not come back, and the command
    must still be pending afterwards.
    """
    tenant = f"tenant-{_uid()}"
    node_name, command_id = await _queue_command_for(client, as_auth, tenant)

    as_auth(tenant, capabilities={"read"})
    resp = await client.post(
        "/api/v1/fleet/heartbeat",
        json={"tenant_id": tenant, "node_name": node_name},
    )
    assert resp.status_code == 403, resp.text
    # The command payload must not have been handed over.
    assert "ping" not in resp.text

    # And the queue must be intact: still pending, never acked.
    as_auth(tenant)
    listed = await client.get(f"/api/v1/fleet/commands?tenant_id={tenant}")
    assert listed.status_code == 200, listed.text
    mine = [c for c in listed.json() if c["id"] == command_id]
    assert mine, f"command {command_id} vanished from the queue"
    assert mine[0]["status"] == "pending", (
        f"command was drained by a read-only caller: status={mine[0]['status']!r}, "
        f"acked_at={mine[0]['acked_at']!r}"
    )
    assert mine[0]["acked_at"] is None


@pytest.mark.parametrize(
    "cred",
    [
        pytest.param({}, id="legacy-key-no-capabilities"),
        pytest.param({"capabilities": {"read", "write"}}, id="read-write-key"),
    ],
)
async def test_heartbeat_still_works_for_a_writing_credential(client, as_auth, cred):
    """OVER-REFUSAL GUARD, and the one that matters most here.

    The heartbeat is how every node stays live and commandable, on a ~60s
    tick. Breaking it for legitimate plugins would take the fleet offline —
    a worse outage than the bug being fixed. Legacy credentials carry
    ``capabilities=None`` and must pass untouched.
    """
    tenant = f"tenant-{_uid()}"
    as_auth(tenant, **cred)
    resp = await client.post(
        "/api/v1/fleet/heartbeat",
        json={
            "tenant_id": tenant,
            "node_name": f"node-{_uid()}",
            "fleet_id": f"fleet-{_uid()}",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True


async def test_heartbeat_delivers_commands_to_a_writing_credential(client, as_auth):
    """OVER-REFUSAL GUARD. The command channel itself must still work.

    Pairs with the drain test above: the same sequence, with a credential
    that may write, must return the payload and ack it. Without this, a gate
    that refused every caller would satisfy the refusal tests.
    """
    tenant = f"tenant-{_uid()}"
    node_name, command_id = await _queue_command_for(client, as_auth, tenant)

    as_auth(tenant)
    resp = await client.post(
        "/api/v1/fleet/heartbeat",
        json={"tenant_id": tenant, "node_name": node_name},
    )
    assert resp.status_code == 200, resp.text
    delivered = [c for c in resp.json()["commands"] if c["id"] == command_id]
    assert delivered, f"command {command_id} was not delivered: {resp.text}"
    assert delivered[0]["command"] == "ping"


# ---------------------------------------------------------------------------
# M-25: DELETE /fleet/{fleet_id} accepted an agent-scoped credential.
#
# ``enforce_not_agent_credential`` names fleet operations as admin-plane in its
# own docstring, and the sibling ``POST /fleet/{fleet_id}/purge`` calls it. This
# route carried the policy everywhere except the line enforcing it.
#
# These assert on STATE, not only the status code: without the guard the call
# does not merely return 2xx, it actually deletes the fleet's node rows. A
# status-only test would still pass against a version that refused the response
# after doing the work.
# ---------------------------------------------------------------------------


async def _seed_fleet(client, as_auth, tenant: str) -> str:
    """Heartbeat one node into a fresh fleet; return the fleet_id."""
    return (await _seed_node(client, as_auth, tenant)).fleet_id


async def _node_count(client, as_auth, tenant: str, fleet_id: str) -> int:
    """Nodes still in ``fleet_id``. Scoped to the fleet, so it says what it means.

    Re-arms the tenant credential first: the caller under test may hold an
    agent-scoped one, which is not what should be reading this back.
    """
    as_auth(tenant)
    resp = await client.get(
        f"/api/v1/fleet/nodes?tenant_id={tenant}&fleet_id={fleet_id}"
    )
    assert resp.status_code == 200, resp.text
    return len(resp.json())


async def test_agent_credential_cannot_delete_a_fleet(client, as_auth):
    """The finding. The key is scoped to one agent; the path param is any fleet."""
    tenant = f"tenant-{_uid()}"
    fleet_id = await _seed_fleet(client, as_auth, tenant)

    as_auth(tenant, agent_id=f"agent-{_uid()}")
    resp = await client.delete(f"/api/v1/fleet/{fleet_id}?tenant_id={tenant}")
    assert resp.status_code == 403, resp.text

    assert await _node_count(client, as_auth, tenant, fleet_id) == 1, (
        f"fleet {fleet_id} was deleted despite the refusal — the guard has to "
        "run before the storage call, not after it"
    )


async def test_a_tenant_credential_can_still_delete_a_fleet(client, as_auth):
    """OVER-REFUSAL GUARD. Blocking every caller would satisfy the test above."""
    tenant = f"tenant-{_uid()}"
    fleet_id = await _seed_fleet(client, as_auth, tenant)

    as_auth(tenant)
    resp = await client.delete(f"/api/v1/fleet/{fleet_id}?tenant_id={tenant}")
    assert resp.status_code == 204, resp.text

    assert await _node_count(client, as_auth, tenant, fleet_id) == 0, (
        "the fleet's node survived a delete by a credential that may delete it"
    )


# ---------------------------------------------------------------------------
# POST /fleet/commands accepted an agent-scoped credential.
#
# Same missing gate as M-25 above, on the route that dispatches TO a node
# rather than deleting one. Why the other two gates do not narrow this caller,
# and what a ``deploy`` payload reaches on the node, is on ``create_command``
# in ``core_api/routes/fleet.py`` — not restated here.
#
# These assert the ERROR CODE, not just the status. All three of this route's
# gates answer 403 (bar ``enforce_tenant``'s no-tenant 400), so a status-only
# test would pass against a version that refused for the wrong reason — or one
# that refuses every caller.
# ---------------------------------------------------------------------------


async def test_agent_credential_cannot_queue_a_fleet_command(client, as_auth):
    """The finding. The payload is the code the node would go on to run."""
    tenant = f"tenant-{_uid()}"
    node_id = (await _seed_node(client, as_auth, tenant)).node_id

    as_auth(tenant, agent_id=f"agent-{_uid()}")
    resp = await client.post(
        "/api/v1/fleet/commands",
        json={
            "tenant_id": tenant,
            "node_id": node_id,
            "command": "deploy",
            "payload": {"source": "export const injected = 1;"},
        },
    )
    assert resp.status_code == 403, resp.text
    code = resp.json()["error"]["code"]
    assert code == errors.AUTH_AGENT_CREDENTIAL_FORBIDDEN, resp.text

    # The status is not the property that matters — assert the row was never
    # written, so a gate placed AFTER the storage call still fails here.
    #
    # This listing settles delivery too, without a second heartbeat.
    # ``fleet_list_commands`` filters on ``tenant_id`` alone (no status, limit
    # 50), while ``fleet_get_pending_commands`` filters the (node, tenant,
    # status='pending') triple — so an empty listing for a fresh tenant is the
    # strict superset of what any node in it could be handed.
    as_auth(tenant)
    listed = await client.get(f"/api/v1/fleet/commands?tenant_id={tenant}")
    assert listed.status_code == 200, listed.text
    assert listed.json() == [], f"the refused command was queued anyway: {listed.text}"


async def test_a_tenant_credential_can_still_queue_a_fleet_command(client, as_auth):
    """OVER-REFUSAL GUARD, and it is load-bearing.

    This is the dashboard's own dispatch path — an operator restarting a node
    or pushing a deploy. Refusing every caller would satisfy the test above and
    leave the fleet uncommandable, which is worse than the bug being fixed.
    """
    tenant = f"tenant-{_uid()}"
    node_id = (await _seed_node(client, as_auth, tenant)).node_id

    as_auth(tenant)
    resp = await client.post(
        "/api/v1/fleet/commands",
        json={
            "tenant_id": tenant,
            "node_id": node_id,
            "command": "deploy",
            "payload": {"source": "export const legitimate = 1;"},
        },
    )
    assert resp.status_code == 201, resp.text

    listed = await client.get(f"/api/v1/fleet/commands?tenant_id={tenant}")
    assert listed.status_code == 200, listed.text
    assert [c["id"] for c in listed.json()] == [resp.json()["id"]], (
        f"a legitimate command did not reach the queue: {listed.text}"
    )
