"""E2E REST tests for the keystones surface (CAURA-000).

The keystone REST API on core-api proxies to core-storage's
``/api/v1/storage/keystones`` and adds trust enforcement (≥2 to author)
plus audit. Storage-level shape validation is tested in PR1; this file
focuses on the core-api wrapper: trust gate, scope-merge passthrough,
audit emission, and the X-Truncated header.
"""

from __future__ import annotations

import pytest

from tests.conftest import get_test_auth, uid as _uid

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_trusted_agent(client, tenant_id, headers, agent_id, fleet_id):
    """Auto-create an agent and promote it to trust_level=2.

    Writing a memory auto-creates the agent at the default trust
    (=1). Keystone authoring requires trust ≥ 2, so this helper
    follows up with a PATCH to lift the agent to the elevated tier —
    same pattern callers in test_api_agents.py exercise.
    """
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "memory_type": "fact",
            "content": f"seed memory for {agent_id}",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    bump = await client.patch(
        f"/api/v1/agents/{agent_id}?tenant_id={tenant_id}",
        json={"trust_level": 2},
        headers=headers,
    )
    assert bump.status_code == 200, bump.text


def _author_headers(headers: dict, agent_id: str) -> dict:
    """Pin the request's agent identity so the trust check resolves
    against a real, seeded agent rather than the admin-key fallback."""
    return {**headers, "X-Agent-ID": agent_id}


async def _set_keystone(client, headers, tenant_id, **overrides):
    """POST a tenant-scope keystone with sensible defaults; overrides win."""
    payload = {
        "tenant_id": tenant_id,
        "doc_id": overrides.pop("doc_id", f"ks-{_uid()}"),
        "title": "No secrets",
        "content": "Never commit credentials.",
        "scope": "tenant",
        "weight": "med",
    }
    payload.update(overrides)
    resp = await client.post("/api/v1/memclaw/keystones", json=payload, headers=headers)
    return resp


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def test_list_keystones_empty(client):
    """GET returns an empty list for a tenant with no keystones — no 500."""
    tenant_id, headers = get_test_auth()
    resp = await client.get(
        f"/api/v1/memclaw/keystones?tenant_id={tenant_id}", headers=headers
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


# ---------------------------------------------------------------------------
# Trust gate (write)
# ---------------------------------------------------------------------------


async def test_set_rejected_when_agent_unknown(client):
    """Writing as an agent that doesn't exist must 403 — the trust check
    treats not_found as a hard reject, preventing prompt-injection-driven
    rule planting through unseeded identities."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    resp = await _set_keystone(
        client,
        _author_headers(headers, f"ghost-{tag}"),
        tenant_id,
        doc_id=f"ks-{tag}",
    )
    assert resp.status_code == 403, resp.text


async def test_set_rejected_for_default_trust_agent(client):
    """An agent registered at the default trust level (=1) must NOT be
    able to author a keystone. Keystones override user instructions
    across the tenant; the gate is trust ≥ 2 (elevated tier)."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent_id = f"default-trust-{tag}"
    # Seed the agent via the auto-create-on-first-write path — leaves
    # trust at the default 1 (no follow-up PATCH).
    seed = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "fleet_id": f"fleet-{tag}",
            "memory_type": "fact",
            "content": f"seed for {agent_id}",
        },
        headers=headers,
    )
    assert seed.status_code == 201, seed.text

    resp = await _set_keystone(
        client,
        _author_headers(headers, agent_id),
        tenant_id,
        doc_id=f"ks-{tag}",
    )
    assert resp.status_code == 403, resp.text


async def test_set_allowed_for_trusted_agent(client):
    """A seeded agent promoted to trust_level=2 can author a keystone."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent_id = f"author-{tag}"
    await _seed_trusted_agent(client, tenant_id, headers, agent_id, f"fleet-{tag}")

    resp = await _set_keystone(
        client,
        _author_headers(headers, agent_id),
        tenant_id,
        doc_id=f"ks-{tag}",
        weight="high",
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["doc_id"] == f"ks-{tag}"
    assert body["data"]["scope"] == "tenant"
    assert body["data"]["weight"] == 100  # 'high' bucket → 100 at storage


# ---------------------------------------------------------------------------
# Round-trip + scope merge passthrough
# ---------------------------------------------------------------------------


async def test_set_then_list_round_trip(client):
    """POSTed keystone shows up in GET for the same tenant."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent_id = f"author-{tag}"
    fleet_id = f"fleet-{tag}"
    await _seed_trusted_agent(client, tenant_id, headers, agent_id, fleet_id)

    doc_id = f"ks-{tag}"
    set_resp = await _set_keystone(
        client,
        _author_headers(headers, agent_id),
        tenant_id,
        doc_id=doc_id,
        title="Round trip",
        content="reachable via GET",
    )
    assert set_resp.status_code == 200, set_resp.text

    get_resp = await client.get(
        f"/api/v1/memclaw/keystones?tenant_id={tenant_id}", headers=headers
    )
    assert get_resp.status_code == 200
    rules = get_resp.json()
    assert any(r["doc_id"] == doc_id for r in rules), rules


# ---------------------------------------------------------------------------
# Storage-side validation surfaces as 422
# ---------------------------------------------------------------------------


async def test_set_invalid_scope_surfaces_as_422(client):
    """The storage validator owns scope/weight shape rules; the proxy
    must surface its 422 (not silently swallow or 500)."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent_id = f"author-{tag}"
    await _seed_trusted_agent(client, tenant_id, headers, agent_id, f"fleet-{tag}")

    # `scope=tenant` with a fleet_id is rejected at the storage validator.
    resp = await client.post(
        "/api/v1/memclaw/keystones",
        json={
            "tenant_id": tenant_id,
            "fleet_id": f"fleet-{tag}",
            "doc_id": f"ks-{tag}",
            "title": "Bad scope",
            "content": "...",
            "scope": "tenant",
            "weight": "low",
        },
        headers=_author_headers(headers, agent_id),
    )
    # Pydantic on our side accepts the shape (literals match), so the
    # call reaches storage which 422s on scope=tenant+fleet_id mismatch.
    # ``_surface_storage_error`` translates storage's HTTPStatusError
    # into a 422 here (rather than letting it bubble as a 500).
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def test_delete_round_trip(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent_id = f"author-{tag}"
    await _seed_trusted_agent(client, tenant_id, headers, agent_id, f"fleet-{tag}")

    doc_id = f"ks-{tag}"
    set_resp = await _set_keystone(
        client, _author_headers(headers, agent_id), tenant_id, doc_id=doc_id
    )
    assert set_resp.status_code == 200, set_resp.text

    del_resp = await client.delete(
        f"/api/v1/memclaw/keystones/{doc_id}?tenant_id={tenant_id}",
        headers=_author_headers(headers, agent_id),
    )
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True

    # Second delete is a clean 404 (not 500).
    del_resp2 = await client.delete(
        f"/api/v1/memclaw/keystones/{doc_id}?tenant_id={tenant_id}",
        headers=_author_headers(headers, agent_id),
    )
    assert del_resp2.status_code == 404


async def test_delete_requires_trust(client):
    """DELETE is trust-gated. The handler resolves the lookup first
    (so missing-rule yields 404 without revealing authz state to a
    probing caller); when the rule DOES exist, an unseeded ``ghost``
    agent is rejected with 403."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    # Seed a tenant-scope rule via a trusted author so the row exists.
    admin = f"admin-{tag}"
    await _seed_trusted_agent(client, tenant_id, headers, admin, f"fleet-{tag}")
    doc_id = f"ks-{tag}"
    set_resp = await _set_keystone(
        client, _author_headers(headers, admin), tenant_id, doc_id=doc_id
    )
    assert set_resp.status_code == 200, set_resp.text

    # Unseeded ``ghost`` agent tries to delete the existing rule.
    resp = await client.delete(
        f"/api/v1/memclaw/keystones/{doc_id}?tenant_id={tenant_id}",
        headers=_author_headers(headers, f"ghost-{tag}"),
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Tiered trust (agent self-author at ≥ 1; fleet/tenant/cross-agent at ≥ 2)
# ---------------------------------------------------------------------------


async def _seed_default_trust_agent(client, tenant_id, headers, agent_id, fleet_id):
    """Create an agent at the auto-default trust (=1) — no PATCH bump."""
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "memory_type": "fact",
            "content": f"seed memory for {agent_id}",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text


async def test_set_agent_scope_self_unverified_rejected(client):
    """Anti-spoof: a self-author claim via X-Agent-ID alone (no
    gateway-verified ``auth.agent_id``) is the spoofing surface — an
    admin-key holder could supply any victim's id and forge a rule in
    that victim's name at trust 1. The OSS test path is admin-keyed,
    so ``caller_verified`` is False and ``_effective_min_for_caller``
    bumps the floor to ≥ 2; a trust-1 agent attempting self-author
    through this path is rejected. To exercise the legitimate
    self-author tier in production, callers need a gateway-verified
    agent identity (e.g. an ``mca_*`` agent-scoped key)."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent_id = f"self-{tag}"
    fleet_id = f"fleet-{tag}"
    await _seed_default_trust_agent(client, tenant_id, headers, agent_id, fleet_id)

    resp = await _set_keystone(
        client,
        _author_headers(headers, agent_id),
        tenant_id,
        doc_id=f"ks-{tag}",
        scope="agent",
        fleet_id=fleet_id,
        agent_id=agent_id,
        weight="med",
    )
    assert resp.status_code == 403, resp.text


async def test_xagent_id_mismatch_with_auth_rejected(client):
    """When both ``auth.agent_id`` and ``X-Agent-ID`` are set but
    disagree, the request is rejected as a spoofing attempt rather
    than letting the helper silently pick one."""
    # The OSS test infrastructure can't populate ``auth.agent_id``
    # via the admin-key path (the admin path discards X-Agent-ID
    # before constructing AuthContext). To prove the helper raises,
    # we call it directly with a synthetic AuthContext.
    from core_api.auth import AuthContext
    from core_api.routes.keystones import _resolve_caller_identity
    from fastapi import HTTPException

    auth = AuthContext(tenant_id="t", agent_id="alice")
    try:
        _resolve_caller_identity(auth, "bob")
    except HTTPException as exc:
        assert exc.status_code == 403
        assert "does not match" in str(exc.detail).lower()
    else:
        raise AssertionError("expected HTTPException(403) on mismatch")


async def test_set_agent_scope_other_at_trust_1_rejected(client):
    """A trust-1 agent cannot author scope=agent for someone else."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    me = f"me-{tag}"
    other = f"other-{tag}"
    fleet_id = f"fleet-{tag}"
    await _seed_default_trust_agent(client, tenant_id, headers, me, fleet_id)

    resp = await _set_keystone(
        client,
        _author_headers(headers, me),
        tenant_id,
        doc_id=f"ks-{tag}",
        scope="agent",
        fleet_id=fleet_id,
        agent_id=other,
        weight="med",
    )
    assert resp.status_code == 403, resp.text


async def test_set_fleet_scope_at_trust_1_rejected(client):
    """scope=fleet stays at trust ≥ 2."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent_id = f"author-{tag}"
    fleet_id = f"fleet-{tag}"
    await _seed_default_trust_agent(client, tenant_id, headers, agent_id, fleet_id)

    resp = await _set_keystone(
        client,
        _author_headers(headers, agent_id),
        tenant_id,
        doc_id=f"ks-{tag}",
        scope="fleet",
        fleet_id=fleet_id,
        weight="med",
    )
    assert resp.status_code == 403, resp.text


async def test_set_tenant_scope_at_trust_1_rejected(client):
    """scope=tenant stays at trust ≥ 2."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent_id = f"author-{tag}"
    fleet_id = f"fleet-{tag}"
    await _seed_default_trust_agent(client, tenant_id, headers, agent_id, fleet_id)

    resp = await _set_keystone(
        client,
        _author_headers(headers, agent_id),
        tenant_id,
        doc_id=f"ks-{tag}",
        scope="tenant",
        weight="med",
    )
    assert resp.status_code == 403, resp.text


async def test_delete_own_agent_rule_unverified_rejected(client):
    """Same anti-spoof rationale as
    ``test_set_agent_scope_self_unverified_rejected``: deleting a
    ``scope=agent`` rule via the admin-key OSS path is treated as
    unverified caller identity, so the floor bumps to ≥ 2 and a
    trust-1 attempt fails. Authoring the seed rule itself also fails
    (same anti-spoof) — seed via a trust-2 admin instead so the row
    exists for the deletion attempt to exercise the gate.
    """
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet_id = f"fleet-{tag}"
    admin = f"admin-{tag}"
    await _seed_trusted_agent(client, tenant_id, headers, admin, fleet_id)
    doc_id = f"ks-{tag}"
    # Author the rule via admin (trust 2) so it exists. The admin path
    # is also unverified, but trust 2 clears the bumped floor (max(2, 2)).
    set_resp = await _set_keystone(
        client,
        _author_headers(headers, admin),
        tenant_id,
        doc_id=doc_id,
        scope="agent",
        fleet_id=fleet_id,
        agent_id=admin,
        weight="med",
    )
    assert set_resp.status_code == 200, set_resp.text

    # Seed the trust-1 agent who tries to delete.
    member = f"self-{tag}"
    await _seed_default_trust_agent(client, tenant_id, headers, member, fleet_id)
    del_resp = await client.delete(
        f"/api/v1/memclaw/keystones/{doc_id}?tenant_id={tenant_id}",
        headers=_author_headers(headers, member),
    )
    # Member isn't the rule's owner (admin is) AND the path is unverified
    # — both reasons land on 403. Either way: trust-1 unverified can't
    # delete a ``scope=agent`` rule.
    assert del_resp.status_code == 403, del_resp.text


async def test_overwrite_fleet_rule_as_self_agent_rejected(client):
    """Privilege escalation guard: a trust-1 agent must NOT be able to
    overwrite an existing scope=fleet rule by sending scope=agent +
    agent_id=<self> in the body. The effective floor takes the max of
    the new shape (1) and the stored shape (2), so the gate fires."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet_id = f"fleet-{tag}"
    # Trust-2 admin authors a fleet rule under doc_id ``ks-{tag}``.
    admin = f"admin-{tag}"
    await _seed_trusted_agent(client, tenant_id, headers, admin, fleet_id)
    doc_id = f"ks-{tag}"
    set_resp = await _set_keystone(
        client,
        _author_headers(headers, admin),
        tenant_id,
        doc_id=doc_id,
        scope="fleet",
        fleet_id=fleet_id,
        weight="med",
    )
    assert set_resp.status_code == 200, set_resp.text

    # Trust-1 member attempts to overwrite under the same doc_id by
    # claiming scope=agent + agent_id=<self>.
    attacker = f"attacker-{tag}"
    await _seed_default_trust_agent(client, tenant_id, headers, attacker, fleet_id)
    resp = await _set_keystone(
        client,
        _author_headers(headers, attacker),
        tenant_id,
        doc_id=doc_id,  # same doc_id as the fleet rule
        scope="agent",
        fleet_id=fleet_id,
        agent_id=attacker,
        weight="med",
    )
    assert resp.status_code == 403, resp.text


async def test_delete_fleet_rule_at_trust_1_rejected(client):
    """A trust-1 agent cannot delete a scope=fleet rule even within its fleet."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    fleet_id = f"fleet-{tag}"
    # Trust-2 admin authors the fleet rule.
    admin = f"admin-{tag}"
    await _seed_trusted_agent(client, tenant_id, headers, admin, fleet_id)
    doc_id = f"ks-{tag}"
    set_resp = await _set_keystone(
        client,
        _author_headers(headers, admin),
        tenant_id,
        doc_id=doc_id,
        scope="fleet",
        fleet_id=fleet_id,
        weight="med",
    )
    assert set_resp.status_code == 200, set_resp.text

    # Trust-1 member of the same fleet tries to delete it.
    member = f"member-{tag}"
    await _seed_default_trust_agent(client, tenant_id, headers, member, fleet_id)
    del_resp = await client.delete(
        f"/api/v1/memclaw/keystones/{doc_id}?tenant_id={tenant_id}",
        headers=_author_headers(headers, member),
    )
    assert del_resp.status_code == 403, del_resp.text
