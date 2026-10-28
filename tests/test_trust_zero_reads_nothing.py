"""An agent at trust level 0 reads nothing, as the trust ladder documents.

Level 0 is "restricted: no read or write access" (static/docs/integration-guide.md),
the way to disable an agent, and the level ``agents.require_agent_approval`` gives
a new agent until an admin approves it. Writes refuse it, and so do ``caura_list``
/ ``caura_stats``, the scoped ``GET /memories`` and the STM bulletins. Search,
recall, the unscoped list, stats, count and map, and a read by id must refuse it
too, or one of them is the way round the others.

Each read is asked at trust 0 and at trust 1, so a gate that refused everyone
would fail here as surely as a missing one. A signed-in person who filters by a
restricted agent is not that agent, and keeps reading.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock

import pytest

from core_api import mcp_server
from core_api.errors import AUTH_AGENT_TRUST_TOO_LOW
from tests._mcp_test_helpers import parse_envelope, stub_storage_client
from tests.conftest import get_test_auth, new_tenant_id, uid

_READS = [
    ("search", "POST", "/api/v1/search"),
    ("recall", "POST", "/api/v1/recall"),
    ("list", "GET", "/api/v1/memories"),
    ("stats", "GET", "/api/v1/memories/stats"),
    ("count", "GET", "/api/v1/memories/count"),
    ("map", "GET", "/api/v1/memories/field"),
]


@contextlib.contextmanager
def _as(**auth):
    """Run the enclosed requests under one ``AuthContext``."""
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context

    app.dependency_overrides[get_auth_context] = lambda: AuthContext(**auth)
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_auth_context, None)


async def _set_trust(
    sc, tenant_id: str, agent_id: str, fleet_id: str, trust: int
) -> None:
    await sc.create_or_update_agent(
        {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "trust_level": trust,
        }
    )


@pytest.fixture
async def fleet(client, sc):
    """One fleet with a shared row a peer wrote, and the reader registered in it."""
    tenant_id, headers = get_test_auth(new_tenant_id())
    tag = uid()
    names = {
        "reader": f"reader-{tag}",
        "writer": f"writer-{tag}",
        "fleet": f"fleet-{tag}",
    }
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": names["writer"],
            "fleet_id": names["fleet"],
            "content": f"The deploy window for the billing service is Tuesday [{tag}]",
            "memory_type": "fact",
            "visibility": "scope_team",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    names["memory_id"] = resp.json()["id"]
    return tenant_id, names


async def _read(
    client, tenant_id: str, method: str, path: str, *, agent: str | None = None
):
    if method == "POST":
        body = {"tenant_id": tenant_id, "query": "deploy window billing"}
        if agent is not None:
            body["filter_agent_id"] = agent
        return await client.post(path, json=body)
    query = f"?tenant_id={tenant_id}" + (
        f"&agent_id={agent}" if agent is not None else ""
    )
    return await client.get(path + query)


@pytest.mark.parametrize("trust", [0, 1])
@pytest.mark.parametrize(
    ("case", "method", "path"), _READS, ids=[c for c, _, _ in _READS]
)
async def test_an_agent_credential_at_trust_0_is_refused_every_read(
    client, sc, fleet, trust, case, method, path
):
    tenant_id, names = fleet
    await _set_trust(sc, tenant_id, names["reader"], names["fleet"], trust)

    with _as(
        tenant_id=tenant_id, agent_id=names["reader"], readable_tenant_ids=[tenant_id]
    ):
        resp = await _read(client, tenant_id, method, path)

    if trust == 0:
        assert resp.status_code == 403, f"{case}: {resp.status_code} {resp.text[:300]}"
        assert resp.json()["error"]["code"] == AUTH_AGENT_TRUST_TOO_LOW, case
    else:
        assert resp.status_code == 200, f"{case}: {resp.status_code} {resp.text[:300]}"


@pytest.mark.parametrize("trust", [0, 1])
async def test_a_read_by_id_at_trust_0_finds_nothing(client, sc, fleet, trust):
    """By id the answer is the one any row the caller may not read gets: 404."""
    tenant_id, names = fleet
    await _set_trust(sc, tenant_id, names["reader"], names["fleet"], trust)

    with _as(
        tenant_id=tenant_id, agent_id=names["reader"], readable_tenant_ids=[tenant_id]
    ):
        resp = await client.get(
            f"/api/v1/memories/{names['memory_id']}?tenant_id={tenant_id}"
        )

    assert resp.status_code == (404 if trust == 0 else 200), resp.text[:300]


async def test_a_restricted_agent_cannot_read_even_its_own_private_row(
    client, sc, fleet
):
    tenant_id, names = fleet
    _, headers = get_test_auth(tenant_id)
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": names["reader"],
            "fleet_id": names["fleet"],
            "content": f"My own scratch note on the billing deploy [{uid()}]",
            "memory_type": "fact",
            "visibility": "scope_agent",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    await _set_trust(sc, tenant_id, names["reader"], names["fleet"], 0)

    with _as(
        tenant_id=tenant_id, agent_id=names["reader"], readable_tenant_ids=[tenant_id]
    ):
        got = await client.get(
            f"/api/v1/memories/{resp.json()['id']}?tenant_id={tenant_id}"
        )

    assert got.status_code == 404, got.text[:300]


@pytest.mark.parametrize(
    ("case", "method", "path"), _READS, ids=[c for c, _, _ in _READS]
)
async def test_a_tenant_key_reading_as_a_restricted_agent_is_refused(
    client, sc, fleet, case, method, path
):
    """The plugin on a tenant key names its agent in the request; the agent the
    request reads as is the one the ladder ranks, as on the scoped reads."""
    tenant_id, names = fleet
    await _set_trust(sc, tenant_id, names["reader"], names["fleet"], 0)

    with _as(tenant_id=tenant_id, readable_tenant_ids=[tenant_id]):
        resp = await _read(client, tenant_id, method, path, agent=names["reader"])

    if case == "count":
        # Takes no agent: a tenant key's count reads as the tenant.
        assert resp.status_code == 200, resp.text[:300]
    else:
        assert resp.status_code == 403, f"{case}: {resp.status_code} {resp.text[:300]}"


@pytest.mark.parametrize(
    ("case", "method", "path"), _READS, ids=[c for c, _, _ in _READS]
)
async def test_a_person_filtering_by_a_restricted_agent_still_reads(
    client, sc, fleet, case, method, path
):
    """OVER-REFUSAL GUARD: the dashboard lists and counts a disabled agent's rows."""
    tenant_id, names = fleet
    await _set_trust(sc, tenant_id, names["reader"], names["fleet"], 0)

    with _as(tenant_id=tenant_id, readable_tenant_ids=[tenant_id], is_person=True):
        resp = await _read(client, tenant_id, method, path, agent=names["reader"])

    assert resp.status_code == 200, f"{case}: {resp.status_code} {resp.text[:300]}"


async def test_an_unregistered_agent_keeps_its_soft_pass(client, fleet):
    """Unchanged: an identity with no row has no level to be refused at."""
    tenant_id, _names = fleet
    stranger = f"stranger-{uid()}"

    with _as(tenant_id=tenant_id, agent_id=stranger, readable_tenant_ids=[tenant_id]):
        resp = await client.get(f"/api/v1/memories?tenant_id={tenant_id}")

    assert resp.status_code == 200, resp.text[:300]


# --- MCP caura_recall ---------------------------------------------------------


@pytest.mark.parametrize("trust", [0, 1])
async def test_caura_recall_at_trust_0_is_refused_before_it_searches(
    mcp_env, monkeypatch, trust
):
    stub_storage_client(
        monkeypatch,
        get_agent={"agent_id": "reader", "fleet_id": "fleet-a", "trust_level": trust},
    )
    search = AsyncMock(return_value=[])
    monkeypatch.setattr(mcp_server, "search_memories", search, raising=False)

    out = await mcp_server.caura_recall(query="deploy window", agent_id="reader")

    if trust == 0:
        assert parse_envelope(out)["error"]["code"] == AUTH_AGENT_TRUST_TOO_LOW
        search.assert_not_awaited()
        mcp_server.check_and_increment.assert_not_awaited()
    else:
        search.assert_awaited_once()
