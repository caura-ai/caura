"""H-05 — every requested fleet is gated, not just the single-fleet case.

The cross-fleet trust ladder on ``POST /search``, ``POST /recall`` and MCP
``caura_recall`` fired only when ``fleet_ids`` had exactly one element:

    if not body.fleet_ids and _agent.fleet_id and trust < 2:
        body.fleet_ids = [own]              # forcing: only when NONE given
    if body.fleet_ids and len(body.fleet_ids) == 1:
        await enforce_fleet_read(...)       # ladder: only when EXACTLY one

A trust-1 agent sending ``fleet_ids=["victim-fleet", "own-fleet"]`` satisfied
neither branch — the forcing is skipped because the list is non-empty, and the
ladder is skipped because the length is 2. The unchecked list went straight to
the storage predicate, which admits ``fleet_id IN (...)`` and returns
``scope_team`` rows WITH FULL CONTENT for every fleet listed.

So asking for MORE was the way to be asked for LESS: ``["victim"]`` was 403'd
and ``["victim", "own"]`` succeeded. That is the exact escalation the
single-fleet gate exists to stop.

These tests are written so the PRE-FIX answer is the wrong one — a leak of
victim content — rather than asserting a 403 that could pass for unrelated
reasons.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import get_test_auth
from tests.conftest import uid as _uid

pytestmark = pytest.mark.integration


@pytest.fixture
def as_auth(monkeypatch):
    """Authenticate as a specific agent identity within a tenant.

    ``get_test_auth()`` returns the ADMIN key, which resolves to
    ``tenant_id=None`` — and the whole fleet-gate block is behind
    ``if auth.tenant_id:  # skip for admin``. So the admin key cannot exercise
    this gate at all, and a test written with it would pass before and after
    any change. The credential that reaches the gate is an agent-scoped one.
    """
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str, agent_id: str | None = None):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id,
                agent_id=agent_id,
                readable_tenant_ids=[tenant_id],
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    from core_api.app import app as _app
    from core_api.auth import get_auth_context as _gac

    _app.dependency_overrides.pop(_gac, None)


async def _seed_agent(sc, tenant_id: str, agent_id: str, *, fleet_id: str, trust: int):
    await sc.create_or_update_agent(
        {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "trust_level": trust,
        }
    )


async def _write(client, headers, tenant_id, *, content, agent_id, fleet_id):
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "content": content,
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "visibility": "scope_team",
            "memory_type": "fact",
            # Keep embedding/indexing synchronous so the search below is
            # deterministic the moment the 201 lands.
            "write_mode": "strong",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _setup(client, sc):
    """A victim fleet holding a secret, and a trust-1 attacker in another fleet."""
    tenant_id, headers = get_test_auth()
    nonce = f"h05-{uuid.uuid4().hex}"
    victim_fleet = f"victim-fleet-{_uid()}"
    own_fleet = f"own-fleet-{_uid()}"
    attacker = f"attacker-{_uid()}"

    await _write(
        client,
        headers,
        tenant_id,
        content=f"{nonce} victim fleet private team memory",
        agent_id=f"victim-agent-{_uid()}",
        fleet_id=victim_fleet,
    )
    await _seed_agent(sc, tenant_id, attacker, fleet_id=own_fleet, trust=1)
    return tenant_id, headers, nonce, victim_fleet, own_fleet, attacker


# ---------------------------------------------------------------------------
# /search
# ---------------------------------------------------------------------------


async def test_search_single_foreign_fleet_is_refused(client, sc, as_auth):
    """GUARD, NOT EVIDENCE — passes before and after.

    The single-fleet case was already gated. Present so the pair below reads as
    a comparison: this is the answer the multi-fleet form should have given.
    """
    tenant_id, _, nonce, victim_fleet, _own, attacker = await _setup(client, sc)
    as_auth(tenant_id, attacker)

    resp = await client.post(
        "/api/v1/search",
        json={"tenant_id": tenant_id, "query": nonce, "fleet_ids": [victim_fleet]},
    )
    assert resp.status_code == 403, resp.text
    assert "fleet-scope policy" in resp.text


async def test_search_cannot_widen_by_naming_a_second_fleet(client, sc, as_auth):
    """THE LEAK. Fails without the fix: 200 carrying the victim's content.

    Same caller, same victim fleet, one extra element in the list.
    """
    tenant_id, _, nonce, victim_fleet, own_fleet, attacker = await _setup(client, sc)
    as_auth(tenant_id, attacker)

    resp = await client.post(
        "/api/v1/search",
        json={
            "tenant_id": tenant_id,
            "query": nonce,
            "fleet_ids": [victim_fleet, own_fleet],
        },
    )
    assert resp.status_code == 403, (
        f"naming a second fleet skipped the trust ladder; body={resp.text[:400]}"
    )
    assert "fleet-scope policy" in resp.text
    # Asserted on the CONTENT, not only the status: pre-fix this body carried
    # the victim's memory text verbatim, and a future change that answered 200
    # with filtered results instead of 403 must still not leak it.
    assert "victim fleet private team memory" not in resp.text


async def test_search_cannot_widen_by_repeating_the_same_fleet(client, sc, as_auth):
    """Duplicates defeat a length check just as well as distinct ids.

    ``["victim", "victim"]`` is length 2, so the old ``len == 1`` gate skipped
    it while the storage predicate deduplicated it back to the victim fleet.
    Fails without the fix.
    """
    tenant_id, _, nonce, victim_fleet, _own, attacker = await _setup(client, sc)
    as_auth(tenant_id, attacker)

    resp = await client.post(
        "/api/v1/search",
        json={
            "tenant_id": tenant_id,
            "query": nonce,
            "fleet_ids": [victim_fleet, victim_fleet],
        },
    )
    assert resp.status_code == 403, resp.text


async def test_search_own_fleet_listed_twice_still_allowed(client, sc, as_auth):
    """The gate must not over-refuse: every element is the caller's own fleet.

    Guards against "reject any multi-element list for trust < 2", which would
    have closed the hole by breaking a legitimate caller.
    """
    tenant_id, _, nonce, _victim, own_fleet, attacker = await _setup(client, sc)
    as_auth(tenant_id, attacker)

    resp = await client.post(
        "/api/v1/search",
        json={
            "tenant_id": tenant_id,
            "query": nonce,
            "fleet_ids": [own_fleet, own_fleet],
        },
    )
    assert resp.status_code == 200, resp.text


async def test_search_trust_2_may_still_read_across_fleets(client, sc, as_auth):
    """The ladder still GRANTS what it is supposed to grant.

    A cross-fleet read is a trust-2 privilege, not a forbidden operation — the
    fix must gate the multi-fleet form, not remove the capability.
    """
    tenant_id, _, nonce, victim_fleet, own_fleet, attacker = await _setup(client, sc)
    await _seed_agent(sc, tenant_id, attacker, fleet_id=own_fleet, trust=2)
    as_auth(tenant_id, attacker)

    resp = await client.post(
        "/api/v1/search",
        json={
            "tenant_id": tenant_id,
            "query": nonce,
            "fleet_ids": [victim_fleet, own_fleet],
        },
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# /recall — the identical gap, per the finding
# ---------------------------------------------------------------------------


async def test_recall_cannot_widen_by_naming_a_second_fleet(client, sc, as_auth):
    """Fails without the fix, same shape as /search.

    ``/recall`` keys the gate off ``filter_agent_id`` rather than the
    authenticated identity, so the attacker names itself there.
    """
    tenant_id, _, nonce, victim_fleet, own_fleet, attacker = await _setup(client, sc)
    as_auth(tenant_id, attacker)

    resp = await client.post(
        "/api/v1/recall",
        json={
            "tenant_id": tenant_id,
            "query": nonce,
            "filter_agent_id": attacker,
            "fleet_ids": [victim_fleet, own_fleet],
        },
    )
    assert resp.status_code == 403, (
        f"recall skipped the trust ladder; body={resp.text[:400]}"
    )
