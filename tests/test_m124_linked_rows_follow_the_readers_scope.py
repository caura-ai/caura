"""A contradiction's linked rows get the caller's by-id check (M-124).

``GET /memories/{id}/contradictions`` and MCP ``caura_manage op=lineage`` check
that the caller may read the requested memory, then return the row it replaced
and the rows that replaced it, each with the first 200 characters of its
content. Those linked rows went unchecked, and a link is not a grant: Path C
linked a memory with no fleet, which every agent in the tenant may read, to rows
of other fleets (L-147), so both surfaces handed those rows to agents that
``GET /memories/{id}`` refuses. A linked row is now left out unless the caller
may read it by id.
"""

from __future__ import annotations

import uuid

import pytest

from core_api.services import agent_service
from tests._mcp_test_helpers import stub_storage_client
from tests.conftest import get_test_auth, parse_envelope


@pytest.fixture
def patch_lookup(monkeypatch):
    """``lookup_agent`` answers with the caller in ``fleet_id`` at ``trust_level``."""

    def _set(*, fleet_id: str, trust_level: int) -> None:
        async def fake_lookup(_tenant_id, agent_id):
            return {
                "agent_id": agent_id,
                "fleet_id": fleet_id,
                "trust_level": trust_level,
            }

        monkeypatch.setattr(agent_service, "lookup_agent", fake_lookup)

    return _set


@pytest.fixture
def as_agent():
    """Authenticate the REST client as an agent, as the gateway's X-Agent-ID does."""
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str, agent_id: str) -> None:
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id,
                agent_id=agent_id,
                readable_tenant_ids=[tenant_id],
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


def _row(fleet_id: str | None, *, visibility="scope_team", agent_id="alice") -> dict:
    """A memory row as ``get_memory_contradictions`` returns it."""
    return {
        "id": str(uuid.uuid4()),
        "agent_id": agent_id,
        "fleet_id": fleet_id,
        "visibility": visibility,
        "content": f"a row of {fleet_id or 'no fleet'}",
        "status": "conflicted",
        "created_at": "2026-10-07T00:00:00+00:00",
        "deleted_at": None,
        "supersedes_id": None,
    }


async def _lineage(monkeypatch, caller, memory, *, older=None, newer=()):
    """``(superseded_by id, supersessor ids)`` as MCP lineage shows them."""
    from core_api import mcp_server

    bundle = {"memory": memory, "older": older, "supersessors": list(newer)}
    stub_storage_client(monkeypatch, get_memory_contradictions=bundle)
    monkeypatch.setattr(mcp_server, "_get_agent_id", lambda: caller)
    env = parse_envelope(
        await mcp_server.caura_manage(op="lineage", memory_id=memory["id"])
    )
    assert "error" not in env, env
    older_id = (env["superseded_by"] or {}).get("id")
    return older_id, [m["id"] for m in env["supersessors"]]


@pytest.mark.unit
async def test_lineage_leaves_out_another_fleets_rows(
    mcp_env, monkeypatch, patch_lookup
):
    """The requested memory has no fleet, so a trust-1 agent of fleet-beta may
    read it, but not the fleet-alpha rows linked to it."""
    patch_lookup(fleet_id="fleet-beta", trust_level=1)
    older, newer, own = _row("fleet-alpha"), _row("fleet-alpha"), _row("fleet-beta")

    shown = await _lineage(
        monkeypatch, "bob", _row(None), older=older, newer=[newer, own]
    )

    assert shown == (None, [own["id"]])


@pytest.mark.unit
async def test_lineage_leaves_out_another_agents_private_rows(
    mcp_env, monkeypatch, patch_lookup
):
    patch_lookup(fleet_id="fleet-beta", trust_level=3)
    older = _row("fleet-beta", visibility="scope_agent", agent_id="alice")
    theirs = _row("fleet-beta", visibility="scope_agent", agent_id="alice")
    mine = _row("fleet-beta", visibility="scope_agent", agent_id="bob")

    shown = await _lineage(
        monkeypatch, "bob", _row("fleet-beta"), older=older, newer=[theirs, mine]
    )

    assert shown == (None, [mine["id"]])


@pytest.mark.unit
async def test_lineage_still_shows_a_trust_2_agent_another_fleets_rows(
    mcp_env, monkeypatch, patch_lookup
):
    """Trust 2 may read across fleets, so the same links stay in view."""
    patch_lookup(fleet_id="fleet-beta", trust_level=2)
    older, newer = _row("fleet-alpha"), _row("fleet-alpha")

    shown = await _lineage(monkeypatch, "bob", _row(None), older=older, newer=[newer])

    assert shown == (older["id"], [newer["id"]])


async def _write(client, headers, tenant_id, *, agent_id, fleet_id) -> str:
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "content": f"linked row {uuid.uuid4().hex[:8]}",
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "visibility": "scope_team",
            "memory_type": "fact",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.mark.integration
async def test_contradictions_leave_out_another_fleets_rows(
    client, sc, as_agent, patch_lookup
):
    """Both directions: a memory with no fleet replaced one fleet-alpha row and
    was replaced by another. A trust-1 agent of fleet-beta may read it, not them;
    at trust 2 it may read them too."""
    tenant_id, headers = get_test_auth()
    tag = uuid.uuid4().hex[:8]
    alpha = f"fleet-alpha-{tag}"
    older = await _write(
        client, headers, tenant_id, agent_id=f"alice-{tag}", fleet_id=alpha
    )
    middle = await _write(
        client, headers, tenant_id, agent_id=f"carol-{tag}", fleet_id=None
    )
    newer = await _write(
        client, headers, tenant_id, agent_id=f"alice-{tag}", fleet_id=alpha
    )
    await sc.update_memory_status(older, "conflicted", tenant_id=tenant_id)
    await sc.update_memory_status(
        middle, "conflicted", tenant_id=tenant_id, supersedes_id=older
    )
    await sc.update_memory_status(
        newer, "active", tenant_id=tenant_id, supersedes_id=middle
    )
    url = f"/api/v1/memories/{middle}/contradictions?tenant_id={tenant_id}"

    as_agent(tenant_id, f"bob-{tag}")
    patch_lookup(fleet_id=f"fleet-beta-{tag}", trust_level=1)
    resp = await client.get(url)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["superseded_by"] is None
    assert body["superseded_memories"] == []
    assert body["contradictions"] == []
    assert body["detection_status"] == "pending"

    patch_lookup(fleet_id=f"fleet-beta-{tag}", trust_level=2)
    body = (await client.get(url)).json()
    assert body["superseded_by"]["id"] == older
    assert [m["id"] for m in body["superseded_memories"]] == [newer]
