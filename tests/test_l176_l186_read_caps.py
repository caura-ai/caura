"""L-176 and L-186: the graph and the agent list are capped, and say so.

``GET /graph`` returned every entity and relation of a tenant, and ``GET /agents``
every agent, with no cap, no paging and nothing to tell a caller whether it had
the whole set. Eldad's decision (2026-10-12): a default cap meant to sit above
any tenant, with a signal when it bites. The graph keeps its newest entities and
the heaviest relations among them and says ``truncated``; the agent list pages
with ``limit`` and ``offset`` and names the next page in ``X-Next-Offset``.

The graph caps are lowered here so that a handful of rows exceed them.
"""

from __future__ import annotations

import pytest

from core_api import constants
from core_storage_api.services import postgres_service
from tests.conftest import get_admin_headers, new_tenant_id

pytestmark = pytest.mark.asyncio

_NEXT = "X-Next-Offset"


async def _entities(sc, count: int) -> tuple[str, list[str]]:
    """A fresh tenant with ``count`` entities, oldest first."""
    tenant = new_tenant_id()
    ids = []
    for n in range(count):
        created = await sc.create_entity(
            {
                "tenant_id": tenant,
                "entity_type": "concept",
                "canonical_name": f"node {n}",
            }
        )
        ids.append(str(created["id"]))
    return tenant, ids


async def _relate(sc, tenant: str, frm: str, to: str, weight: float, kind: str) -> str:
    created = await sc.create_relation(
        {
            "tenant_id": tenant,
            "from_entity_id": frm,
            "to_entity_id": to,
            "relation_type": kind,
            "weight": weight,
        }
    )
    return str(created["id"])


def _edges(graph: dict) -> list[tuple[str, str]]:
    return [
        (str(r["from_entity_id"]), str(r["to_entity_id"])) for r in graph["relations"]
    ]


# -- L-176: the graph --------------------------------------------------------------


async def test_l176_past_the_entity_cap_the_newest_come_back_marked(sc, monkeypatch):
    tenant, (oldest, middle, newest) = await _entities(sc, 3)
    await _relate(sc, tenant, middle, newest, 0.9, "manages")
    await _relate(sc, tenant, oldest, middle, 0.5, "mentions")
    monkeypatch.setattr(postgres_service, "GRAPH_MAX_ENTITIES", 2, raising=False)

    graph = await sc.get_full_graph(tenant)

    assert graph.get("truncated") is True
    assert {str(e["id"]) for e in graph["entities"]} == {middle, newest}
    # The edge to the node left out is left out with it.
    assert _edges(graph) == [(middle, newest)]


async def test_l176_past_the_relation_cap_the_heaviest_come_back_marked(
    sc, monkeypatch
):
    tenant, (a, b) = await _entities(sc, 2)
    await _relate(sc, tenant, a, b, 0.2, "mentions")
    heavy = await _relate(sc, tenant, b, a, 0.9, "manages")
    monkeypatch.setattr(postgres_service, "GRAPH_MAX_RELATIONS", 1, raising=False)

    graph = await sc.get_full_graph(tenant)

    assert graph.get("truncated") is True
    assert [str(r["id"]) for r in graph["relations"]] == [heavy]
    assert len(graph["entities"]) == 2


async def test_l176_a_graph_within_the_caps_is_whole_and_says_so(client, sc):
    tenant, (a, b) = await _entities(sc, 2)
    await _relate(sc, tenant, a, b, 0.5, "manages")

    resp = await client.get(
        "/api/v1/graph", params={"tenant_id": tenant}, headers=get_admin_headers()
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("truncated") is False
    assert {n["id"] for n in body["nodes"]} == {a, b}
    assert [(e["source"], e["target"]) for e in body["edges"]] == [(a, b)]


async def test_l176_the_graph_route_says_when_it_was_cut_short(client, sc, monkeypatch):
    tenant, (_older, newer) = await _entities(sc, 2)
    monkeypatch.setattr(postgres_service, "GRAPH_MAX_ENTITIES", 1, raising=False)

    resp = await client.get(
        "/api/v1/graph", params={"tenant_id": tenant}, headers=get_admin_headers()
    )

    assert resp.status_code == 200, resp.text
    assert resp.json().get("truncated") is True
    assert [n["id"] for n in resp.json()["nodes"]] == [newer]


# -- L-186: the agent list ---------------------------------------------------------


async def _agents(sc, tenant: str, names: list[str], fleet_id: str | None = None):
    """Registered in the order given, so the list shows them newest first."""
    for name in names:
        await sc.create_or_update_agent(
            {
                "tenant_id": tenant,
                "agent_id": name,
                "trust_level": 1,
                "fleet_id": fleet_id,
            }
        )


async def _list(client, tenant: str, **params):
    resp = await client.get(
        "/api/v1/agents",
        params={"tenant_id": tenant, **params},
        headers=get_admin_headers(),
    )
    assert resp.status_code == 200, resp.text
    return [agent["agent_id"] for agent in resp.json()], resp.headers.get(_NEXT)


async def test_l186_the_agent_list_pages_and_names_the_next_page(client, sc):
    tenant = new_tenant_id()
    await _agents(sc, tenant, ["a1", "a2", "a3"])

    assert await _list(client, tenant, limit=2) == (["a3", "a2"], "2")
    assert await _list(client, tenant, limit=2, offset=2) == (["a1"], None)


async def test_l186_a_fleet_page_is_a_page_of_that_fleet(client, sc):
    """The fleet filter runs before the page is cut; after it, the newer agents
    of another fleet would fill the page and leave it empty."""
    tenant = new_tenant_id()
    await _agents(sc, tenant, ["f1-old", "f1-new"], fleet_id="f1")
    await _agents(sc, tenant, ["f2-a", "f2-b"], fleet_id="f2")

    assert await _list(client, tenant, fleet_id="f1", limit=1) == (["f1-new"], "1")


async def test_l186_a_page_holds_at_most_the_cap(client):
    resp = await client.get(
        "/api/v1/agents",
        params={
            "tenant_id": new_tenant_id(),
            "limit": constants.MAX_AGENT_LIST_LIMIT + 1,
        },
        headers=get_admin_headers(),
    )
    assert resp.status_code == 422, resp.text


async def test_l186_internal_readers_still_get_every_agent(sc):
    """The control: reports and the agent digest read the whole list, so
    storage pages only when asked to."""
    tenant = new_tenant_id()
    await _agents(sc, tenant, ["a1", "a2", "a3"])

    assert [a["agent_id"] for a in await sc.list_agents(tenant)] == ["a3", "a2", "a1"]
