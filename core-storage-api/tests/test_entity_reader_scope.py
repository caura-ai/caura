"""Entity summaries follow the memory read contract for an agent reader.

An entity's ``canonical_name`` is mined from memory text and ``memory_count``
counts the memories behind it. ``GET /entities/{id}`` already hides the
memories an agent may not read; the list, graph and count readers did not, so
an agent could list ``zenith acquisition`` mined from a peer's ``scope_agent``
note and watch its count grow. With ``caller_agent_id`` these readers now keep
only entities linked to a memory the agent may read, and count only those.
The by-id reads take the same reader and answer 404 for an entity the list
hides. Without one (tenant / user / admin credentials) nothing changes.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX, _memory_payload

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _memory(
    client: AsyncClient, tenant: str, *, agent: str, visibility: str, fleet: str = "fleet-x"
) -> str:
    payload = _memory_payload(tenant, fleet)
    payload["agent_id"] = agent
    payload["visibility"] = visibility
    resp = await client.post(f"{PREFIX}/memories", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _entity(client: AsyncClient, tenant: str, name: str) -> str:
    resp = await client.post(
        f"{PREFIX}/entities",
        json={"tenant_id": tenant, "entity_type": "concept", "canonical_name": name},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _link(client: AsyncClient, tenant: str, memory_id: str, entity_id: str) -> None:
    resp = await client.post(
        f"{PREFIX}/entities/links",
        json={"tenant_id": tenant, "memory_id": memory_id, "entity_id": entity_id, "role": "mentions"},
    )
    assert resp.status_code == 200, resp.text


@pytest.fixture
async def graph(client: AsyncClient) -> dict:
    """``secret`` is mined only from agent b's private note; ``shared`` from a
    team row and the same private note; ``other_fleet`` only from a team row
    in a fleet agent a is confined out of."""
    tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"
    private_b = await _memory(client, tenant, agent="b", visibility="scope_agent")
    team = await _memory(client, tenant, agent="b", visibility="scope_team")
    far_team = await _memory(client, tenant, agent="b", visibility="scope_team", fleet="fleet-y")
    secret = await _entity(client, tenant, f"zenith acquisition {uuid.uuid4().hex[:6]}")
    shared = await _entity(client, tenant, f"quarterly plan {uuid.uuid4().hex[:6]}")
    other_fleet = await _entity(client, tenant, f"fleet y roadmap {uuid.uuid4().hex[:6]}")
    await _link(client, tenant, private_b, secret)
    await _link(client, tenant, private_b, shared)
    await _link(client, tenant, team, shared)
    await _link(client, tenant, far_team, other_fleet)
    rel = await client.post(
        f"{PREFIX}/entities/relations",
        json={
            "tenant_id": tenant,
            "from_entity_id": shared,
            "to_entity_id": secret,
            "relation_type": "funds",
            "weight": 0.5,
        },
    )
    assert rel.status_code == 200, rel.text
    return {"tenant": tenant, "secret": secret, "shared": shared, "other_fleet": other_fleet}


class TestEntityReaderScope:
    async def test_list_hides_entities_mined_only_from_unreadable_memories(
        self, client: AsyncClient, graph: dict
    ) -> None:
        tenant = graph["tenant"]
        resp = await client.get(f"{PREFIX}/entities", params={"tenant_id": tenant, "caller_agent_id": "a"})
        assert resp.status_code == 200, resp.text
        ids = {e["id"] for e in resp.json()}
        assert graph["shared"] in ids
        assert graph["other_fleet"] in ids  # not fleet-bound: may cross fleets
        assert graph["secret"] not in ids

        # The author still sees its own entity.
        own = await client.get(f"{PREFIX}/entities", params={"tenant_id": tenant, "caller_agent_id": "b"})
        assert graph["secret"] in {e["id"] for e in own.json()}

        # A tenant credential keeps the full list.
        full = await client.get(f"{PREFIX}/entities", params={"tenant_id": tenant})
        assert {graph["secret"], graph["shared"], graph["other_fleet"]} <= {e["id"] for e in full.json()}

    async def test_fleet_bound_reader_loses_other_fleets_entities(
        self, client: AsyncClient, graph: dict
    ) -> None:
        resp = await client.get(
            f"{PREFIX}/entities",
            params={
                "tenant_id": graph["tenant"],
                "caller_agent_id": "a",
                "caller_fleet_bound": "true",
                "caller_fleet_ids": ["fleet-x"],
            },
        )
        assert resp.status_code == 200, resp.text
        ids = {e["id"] for e in resp.json()}
        assert graph["shared"] in ids
        assert graph["other_fleet"] not in ids

    async def test_count_includes_only_readable_memories(self, client: AsyncClient, graph: dict) -> None:
        body = {"tenant_id": graph["tenant"], "entity_ids": [graph["shared"], graph["secret"]]}
        agent = await client.post(f"{PREFIX}/entities/count-memories", json={**body, "caller_agent_id": "a"})
        assert agent.status_code == 200, agent.text
        assert agent.json().get(graph["shared"]) == 1
        assert graph["secret"] not in agent.json()

        tenant_wide = await client.post(f"{PREFIX}/entities/count-memories", json=body)
        assert tenant_wide.json()[graph["shared"]] == 2

    async def test_graph_drops_hidden_nodes_and_their_edges(self, client: AsyncClient, graph: dict) -> None:
        resp = await client.get(
            f"{PREFIX}/entities/full-graph", params={"tenant_id": graph["tenant"], "caller_agent_id": "a"}
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert graph["secret"] not in {e["id"] for e in data["entities"]}
        assert not data["relations"], "an edge to a hidden node names it"

        full = await client.get(f"{PREFIX}/entities/full-graph", params={"tenant_id": graph["tenant"]})
        assert len(full.json()["relations"]) == 1

    @pytest.mark.parametrize("path", ["", "/with-memories"])
    async def test_a_read_by_id_hides_what_the_list_hides(
        self, client: AsyncClient, graph: dict, path: str
    ) -> None:
        """M-83 (owner decision 2026-10-05): the by-id reads took no reader, so
        an id the list withholds still returned the entity's name and
        attributes. A hidden entity is now a 404, as a missing id is."""
        tenant = graph["tenant"]
        loose = await _entity(client, tenant, f"hand made {uuid.uuid4().hex[:6]}")

        async def status(entity: str, **reader: object) -> int:
            resp = await client.get(
                f"{PREFIX}/entities/{entity}{path}", params={"tenant_id": tenant, **reader}
            )
            return resp.status_code

        assert await status(graph["secret"], caller_agent_id="a") == 404
        assert await status(graph["shared"], caller_agent_id="a") == 200
        assert await status(loose, caller_agent_id="a") == 200  # mined from no memory
        assert await status(graph["secret"], caller_agent_id="b") == 200  # its author
        assert await status(graph["secret"]) == 200  # a tenant credential
        bound = {"caller_agent_id": "a", "caller_fleet_bound": "true", "caller_fleet_ids": ["fleet-x"]}
        assert await status(graph["other_fleet"], **bound) == 404
        assert await status(graph["shared"], **bound) == 200


class TestLinklessEntities:
    """An entity with no memory links at all derives from no memory — a manual
    ``/entities/upsert``, for one — so there is nothing private to hide, and
    agents that build graphs by hand rely on seeing it. Hidden only when it HAS
    links and none of them is to a memory the agent may read."""

    async def test_linkless_entity_stays_visible_to_a_fleet_bound_agent(
        self, client: AsyncClient, graph: dict
    ) -> None:
        tenant = graph["tenant"]
        manual = await _entity(client, tenant, f"hand built {uuid.uuid4().hex[:6]}")
        reader = {
            "tenant_id": tenant,
            "caller_agent_id": "a",
            "caller_fleet_bound": "true",
            "caller_fleet_ids": ["fleet-x"],
        }
        listed = await client.get(f"{PREFIX}/entities", params=reader)
        assert listed.status_code == 200, listed.text
        ids = {e["id"] for e in listed.json()}
        assert manual in ids
        assert graph["secret"] not in ids, "linked only to a peer's private note: still hidden"

        full = await client.get(f"{PREFIX}/entities/full-graph", params=reader)
        node_ids = {e["id"] for e in full.json()["entities"]}
        assert manual in node_ids
        assert graph["secret"] not in node_ids

        counts = await client.post(
            f"{PREFIX}/entities/count-memories",
            json={
                "tenant_id": tenant,
                "entity_ids": [manual],
                "caller_agent_id": "a",
                "caller_fleet_ids": ["fleet-x"],
            },
        )
        assert counts.json().get(manual, 0) == 0
