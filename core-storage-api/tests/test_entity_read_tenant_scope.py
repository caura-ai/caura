"""The entity-by-id reads must be told which tenant is asking.

The read form of GHSA-wgvw-28pq-jc36 on the entity graph, and the read sibling
of ``PATCH /entities/{entity_id}`` (#1119). Three routes shared
``entity_get_by_id``, which fetched by primary key with no predicate at all:

* ``GET /entities/{entity_id}`` --- the whole entity row.
* ``GET /entities/{entity_id}/with-memories`` --- the row plus its memories.
* ``GET /entities/{entity_id}/relations`` --- the row's edges plus each target
  entity in full, so one hop out into the graph.

The last two took an *optional* ``tenant_id`` and fell back to
``entity.tenant_id`` --- the tenant of the row being addressed --- when it was
omitted. That is not a check: it is satisfied by construction for whatever id
the caller supplies, and an attacker closes it by leaving the parameter off.
Both carried an allowlist note calling that fallback "self-authorizing", which
is why the omitted-parameter cases below are the sharpest tests in the file:
they are the exact request the note said was safe.

**Disclosure, not integrity.** Nothing here mutates a row. What leaks is the
entity's own fields, the memories linked to it, and its outgoing edges.

**Filter, not reject.** A foreign id returns the same 404 as an id that does
not exist, so these routes are not an existence oracle for entity UUIDs ---
a 403 would confirm the row is real and simply owned by someone else.
``test_a_foreign_id_is_indistinguishable_from_a_missing_one`` pins it. Every
path here addresses exactly one id, so filtering and rejecting differ only in
the status code; the partial-match question that a bulk id lookup raises does
not arise, and #1162 already answered that one with "filter".

Each assertion below checks the victim's data is ABSENT from the response, not
merely that the call failed --- a status-code-only test would still pass if a
future refactor 404'd while leaking the row in the body.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX, _memory_payload

pytestmark = [pytest.mark.asyncio]


def _tenant() -> str:
    """A tenant id unique to one test and visible to the end-of-run sweep.

    Same prefix contract as the sibling suites: the root suite's
    ``_setup_schema`` teardown cleans with ``tenant_id LIKE 'test-tenant-%'``,
    so a tenant minted with any other prefix is never reclaimed --- which is
    how #858 left 9,186 rows behind. Local to this module rather than
    ``tests.conftest.new_tenant_id``, which is the root suite's fixture file
    and not importable from here.
    """
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


async def _entity(client: AsyncClient, tenant_id: str, name: str | None = None) -> dict:
    resp = await client.post(
        f"{PREFIX}/entities",
        json={
            "tenant_id": tenant_id,
            "entity_type": "person",
            "canonical_name": name or f"ReadScope-{uuid.uuid4().hex[:8]}",
            "attributes": {"secret": "victim-only"},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _memory(client: AsyncClient, tenant_id: str) -> str:
    fleet_id = f"test-fleet-{uuid.uuid4().hex[:8]}"
    resp = await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant_id, fleet_id))
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _link(client: AsyncClient, tenant_id: str, memory_id: str, entity_id: str) -> None:
    resp = await client.post(
        f"{PREFIX}/entities/links",
        json={
            "tenant_id": tenant_id,
            "memory_id": memory_id,
            "entity_id": entity_id,
            "role": "subject",
        },
    )
    assert resp.status_code == 200, resp.text


async def _relation(client: AsyncClient, tenant_id: str, from_id: str, to_id: str) -> None:
    resp = await client.post(
        f"{PREFIX}/entities/relations",
        json={
            "tenant_id": tenant_id,
            "from_entity_id": from_id,
            "relation_type": "works_with",
            "to_entity_id": to_id,
        },
    )
    assert resp.status_code == 200, resp.text


class TestGetEntityByIdRead:
    async def test_a_foreign_entity_is_not_disclosed(self, client: AsyncClient) -> None:
        """The attack, stated as the request an attacker would send."""
        victim, attacker = _tenant(), _tenant()
        row = await _entity(client, victim, name=f"Victim-{uuid.uuid4().hex[:8]}")

        resp = await client.get(f"{PREFIX}/entities/{row['id']}", params={"tenant_id": attacker})

        assert resp.status_code == 404, resp.text
        assert row["canonical_name"] not in resp.text
        assert "victim-only" not in resp.text

    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        """No tenant used to mean "fetch by primary key"; it is now a 422."""
        row = await _entity(client, _tenant(), name=f"Victim-{uuid.uuid4().hex[:8]}")

        resp = await client.get(f"{PREFIX}/entities/{row['id']}")

        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.text
        assert row["canonical_name"] not in resp.text

    async def test_own_entity_is_returned(self, client: AsyncClient) -> None:
        """The supported call still works."""
        tenant = _tenant()
        row = await _entity(client, tenant)

        resp = await client.get(f"{PREFIX}/entities/{row['id']}", params={"tenant_id": tenant})

        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == row["id"]
        assert resp.json()["canonical_name"] == row["canonical_name"]

    async def test_a_foreign_id_is_indistinguishable_from_a_missing_one(self, client: AsyncClient) -> None:
        """Both 404 identically, so this is not an existence oracle."""
        victim, attacker = _tenant(), _tenant()
        row = await _entity(client, victim)

        foreign = await client.get(f"{PREFIX}/entities/{row['id']}", params={"tenant_id": attacker})
        missing = await client.get(f"{PREFIX}/entities/{uuid.uuid4()}", params={"tenant_id": attacker})

        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()


class TestEntityWithMemoriesRead:
    async def test_a_foreign_entity_and_its_memories_are_not_disclosed(self, client: AsyncClient) -> None:
        victim, attacker = _tenant(), _tenant()
        row = await _entity(client, victim, name=f"Victim-{uuid.uuid4().hex[:8]}")
        memory_id = await _memory(client, victim)
        await _link(client, victim, memory_id, row["id"])

        resp = await client.get(
            f"{PREFIX}/entities/{row['id']}/with-memories", params={"tenant_id": attacker}
        )

        assert resp.status_code == 404, resp.text
        assert row["canonical_name"] not in resp.text
        assert memory_id not in resp.text

    async def test_omitting_the_tenant_no_longer_self_authorizes(self, client: AsyncClient) -> None:
        """The request the retired "self-authorizing" note said was safe.

        ``tenant_id or entity.tenant_id`` meant omitting the parameter scoped
        the read to the addressed row's own tenant --- which is to say, not at
        all. This returned the victim's entity and every memory linked to it.
        """
        victim = _tenant()
        row = await _entity(client, victim, name=f"Victim-{uuid.uuid4().hex[:8]}")
        memory_id = await _memory(client, victim)
        await _link(client, victim, memory_id, row["id"])

        resp = await client.get(f"{PREFIX}/entities/{row['id']}/with-memories")

        assert resp.status_code == 422, resp.text
        assert row["canonical_name"] not in resp.text
        assert memory_id not in resp.text

    async def test_own_entity_with_memories_is_returned(self, client: AsyncClient) -> None:
        tenant = _tenant()
        row = await _entity(client, tenant)
        memory_id = await _memory(client, tenant)
        await _link(client, tenant, memory_id, row["id"])

        resp = await client.get(f"{PREFIX}/entities/{row['id']}/with-memories", params={"tenant_id": tenant})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["entity"]["id"] == row["id"]
        assert [e["memory"]["id"] for e in body["linked_memories"]] == [memory_id]


class TestEntityRelationsRead:
    async def test_a_foreign_entitys_graph_is_not_disclosed(self, client: AsyncClient) -> None:
        victim, attacker = _tenant(), _tenant()
        source = await _entity(client, victim, name=f"Source-{uuid.uuid4().hex[:8]}")
        target = await _entity(client, victim, name=f"Target-{uuid.uuid4().hex[:8]}")
        await _relation(client, victim, source["id"], target["id"])

        resp = await client.get(f"{PREFIX}/entities/{source['id']}/relations", params={"tenant_id": attacker})

        assert resp.status_code == 404, resp.text
        assert target["canonical_name"] not in resp.text
        assert target["id"] not in resp.text

    async def test_omitting_the_tenant_no_longer_self_authorizes(self, client: AsyncClient) -> None:
        """Widest of the three: the response carried each target entity in full.

        With the fallback in place this walked one hop out of the addressed row
        and returned the neighbour's whole record, not just an edge.
        """
        victim = _tenant()
        source = await _entity(client, victim, name=f"Source-{uuid.uuid4().hex[:8]}")
        target = await _entity(client, victim, name=f"Target-{uuid.uuid4().hex[:8]}")
        await _relation(client, victim, source["id"], target["id"])

        resp = await client.get(f"{PREFIX}/entities/{source['id']}/relations")

        assert resp.status_code == 422, resp.text
        assert target["canonical_name"] not in resp.text
        assert target["id"] not in resp.text

    async def test_own_relations_are_returned(self, client: AsyncClient) -> None:
        tenant = _tenant()
        source = await _entity(client, tenant)
        target = await _entity(client, tenant)
        await _relation(client, tenant, source["id"], target["id"])

        resp = await client.get(f"{PREFIX}/entities/{source['id']}/relations", params={"tenant_id": tenant})

        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert [r["target"]["id"] for r in rows] == [target["id"]]
        assert rows[0]["relation"]["relation_type"] == "works_with"
