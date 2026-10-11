"""M-104: an agent's stats count its own private notes, as its list shows them.

``GET /memories`` shows an agent credential its own ``scope_agent`` rows.
``GET /memories/stats`` forwarded an identity only when the caller named one in
``agent_id``, so an agent that left it out was counted without its own private
notes: its ``total`` disagreed with the list the route says it summarises, and
``settled`` read true while its own notes still owed work. MCP ``caura_stats``
left them out the same way at ``scope='all'``, where ``caura_list`` keeps them.

Real storage through the in-process bridge. Controls: a peer's private note
stays out, a tenant credential keeps the team-wide aggregate, an agent that
names itself still gets only what it wrote, and ``scope='agent'`` is unchanged.
"""

from __future__ import annotations

import pytest

from core_api import mcp_server
from core_api.app import app
from core_api.auth import AuthContext, get_auth_context
from core_api.constants import VECTOR_DIM
from core_api.tenant_context import set_current_tenant
from tests._mcp_test_helpers import parse_envelope
from tests.conftest import new_tenant_id

pytestmark = pytest.mark.asyncio

OWNER = "m104-owner"
PEER = "m104-peer"


@pytest.fixture
def as_auth():
    def _install(tenant: str, agent_id: str | None = None) -> None:
        async def _dep():
            set_current_tenant(tenant)
            return AuthContext(
                tenant_id=tenant, agent_id=agent_id, readable_tenant_ids=[tenant]
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


async def _tenant(sc) -> str:
    """Each agent wrote one shared and one private note. Only OWNER's private
    note still owes its embedding."""
    tenant = new_tenant_id()
    for agent in (OWNER, PEER):
        for visibility in ("scope_team", "scope_agent"):
            pending = agent == OWNER and visibility == "scope_agent"
            await sc.create_memory(
                {
                    "tenant_id": tenant,
                    "agent_id": agent,
                    "memory_type": "fact",
                    "content": f"{agent} wrote this {visibility} note.",
                    "status": "active",
                    "visibility": visibility,
                    "embedding": None if pending else [0.1] * VECTOR_DIM,
                }
            )
    return tenant


async def _stats(client, tenant: str, **params) -> dict:
    resp = await client.get(
        "/api/v1/memories/stats", params={"tenant_id": tenant, **params}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_an_agents_stats_count_its_own_private_note(client, sc, as_auth):
    tenant = await _tenant(sc)
    as_auth(tenant, agent_id=OWNER)

    listed = await client.get("/api/v1/memories", params={"tenant_id": tenant})
    stats = await _stats(client, tenant)

    assert listed.status_code == 200, listed.text
    # Its own two notes and the peer's shared one: what its list shows.
    assert stats["total"] == len(listed.json()["items"]) == 3
    assert stats["by_agent"] == {OWNER: 2, PEER: 1}
    # Its private note still owes its embedding, so its store is not settled.
    assert stats["pending"]["embedding"] == 1
    assert stats["settled"] is False


@pytest.mark.parametrize(
    ("agent", "params", "by_agent", "settled"),
    [
        (None, {}, {OWNER: 1, PEER: 1}, True),
        (OWNER, {"agent_id": OWNER}, {OWNER: 2}, False),
    ],
    ids=["tenant-credential", "agent-names-itself"],
)
async def test_the_other_stats_reads_are_unchanged(
    client, sc, as_auth, agent, params, by_agent, settled
):
    tenant = await _tenant(sc)
    as_auth(tenant, agent_id=agent)

    stats = await _stats(client, tenant, **params)

    assert stats["by_agent"] == by_agent
    assert stats["total"] == sum(by_agent.values())
    assert stats["settled"] is settled


async def test_a_peer_never_counts_the_owners_private_note(client, sc, as_auth):
    tenant = await _tenant(sc)
    as_auth(tenant, agent_id=PEER)

    stats = await _stats(client, tenant)

    assert stats["by_agent"][OWNER] == 1


@pytest.fixture
def as_mcp_agent(monkeypatch):
    async def _trusted(tenant_id, agent_id, min_level):
        return 3, False, None

    def _install(tenant: str, agent_id: str) -> None:
        monkeypatch.setattr(mcp_server, "_check_auth", lambda: None)
        monkeypatch.setattr(mcp_server, "_get_tenant", lambda: tenant)
        monkeypatch.setattr(
            mcp_server, "_get_agent_id", lambda: mcp_server.AgentIdentity(agent_id)
        )
        monkeypatch.setattr(mcp_server, "_require_trust", _trusted)

    return _install


@pytest.mark.parametrize(
    ("scope", "by_agent"),
    [("all", {OWNER: 2, PEER: 1}), ("agent", {OWNER: 2})],
    ids=["all", "agent"],
)
async def test_mcp_stats_count_the_callers_private_note(
    sc, as_mcp_agent, scope, by_agent
):
    tenant = await _tenant(sc)
    as_mcp_agent(tenant, OWNER)

    stats = parse_envelope(await mcp_server.caura_stats(scope=scope))

    assert stats["by_agent"] == by_agent
    assert stats["total"] == sum(by_agent.values())
