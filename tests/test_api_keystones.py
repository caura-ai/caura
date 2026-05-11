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
    """DELETE is also trust-gated — unseeded agent gets 403, not 404."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    resp = await client.delete(
        f"/api/v1/memclaw/keystones/anything?tenant_id={tenant_id}",
        headers=_author_headers(headers, f"ghost-{tag}"),
    )
    assert resp.status_code == 403, resp.text
