"""The three live reads over ``memory_entity_links`` must be told whose links to return.

Read siblings of #1085 / #1124, whose write halves landed in #1148 and #1152.
All three took bare UUIDs and read the tenantless join table with no predicate:

* ``POST /memories/entity-links`` — a memory's entity graph, by memory id
* ``POST /entities/memory-ids-by-entity-ids`` — memory ids, by entity id
* ``POST /entities/count-memories`` — link counts, by entity id

On a service that authenticates no request (GHSA-wgvw-28pq-jc36), published by
docker-compose on 0.0.0.0:8002, knowing a UUID was enough to read the graph
around it. These are disclosure rather than integrity — nothing here lets a
caller change a row — but the counts and id lists are data about rows the caller
cannot otherwise read, and the third one hands back memory ids its caller then
fetches.

**A row is visible to a tenant exactly when BOTH its ends belong to that
tenant** — the same invariant the write side now enforces, so a link this tenant
could not have created is a link it cannot read. Requiring both ends is what
makes it safe on historical data: rows predating #1148 / #1152 can still
straddle two tenants, and returning one would hand back the other tenant's
memory or entity UUID.

Both ends therefore get their own case per route. The named end is the obvious
one; the *other* end is the one a single-direction test would miss, and each
fails on its own when its half of ``_link_within_tenant`` is reverted.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX, _memory_payload

pytestmark = [pytest.mark.asyncio]


def _tenant() -> str:
    """A tenant id unique to one test and visible to the end-of-run sweep.

    The ``test-tenant-`` prefix is not cosmetic: the root suite's
    ``_setup_schema`` teardown cleans with ``tenant_id LIKE 'test-tenant-%'``
    (reaching this table through the ``memories`` subquery), so a tenant minted
    with any other prefix is never reclaimed — which is how #858 left 9,186 rows
    behind.
    """
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


async def _memory(client: AsyncClient, tenant_id: str) -> str:
    fleet_id = f"test-fleet-{uuid.uuid4().hex[:8]}"
    resp = await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant_id, fleet_id))
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _entity(client: AsyncClient, tenant_id: str) -> str:
    resp = await client.post(
        f"{PREFIX}/entities",
        json={
            "tenant_id": tenant_id,
            "entity_type": "person",
            "canonical_name": f"LinkRead-{uuid.uuid4().hex[:8]}",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _force_link(memory_id: str, entity_id: str, role: str = "subject") -> None:
    """Insert a link row directly, bypassing the scoped write path.

    The whole point of these tests is a link whose two ends are in different
    tenants, and #1148 / #1152 made that unwriteable through the API — correctly.
    Historical rows like this exist in databases that predate those fixes, so the
    row is fabricated in SQL to reproduce the state the reads must not disclose.
    """
    from sqlalchemy import text

    from core_storage_api.services.postgres_service import get_session

    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO memory_entity_links (memory_id, entity_id, role) "
                "VALUES (CAST(:mid AS uuid), CAST(:eid AS uuid), :role) "
                "ON CONFLICT DO NOTHING"
            ),
            {"mid": memory_id, "eid": entity_id, "role": role},
        )


class TestMemoryEntityLinksRead:
    """``POST /memories/entity-links``."""

    async def _read(self, client: AsyncClient, memory_id: str, tenant_id: str) -> dict:
        resp = await client.post(
            f"{PREFIX}/memories/entity-links",
            json={"memory_ids": [memory_id], "tenant_id": tenant_id},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def test_a_foreign_memory_yields_nothing(self, client: AsyncClient) -> None:
        """Named end: asking for someone else's memory returns no links."""
        victim, attacker = _tenant(), _tenant()
        victim_memory = await _memory(client, victim)
        victim_entity = await _entity(client, victim)
        await _force_link(victim_memory, victim_entity)

        body = await self._read(client, victim_memory, attacker)

        assert body.get(victim_memory, []) == [], "another tenant's entity graph was returned"

    async def test_a_foreign_entity_is_not_disclosed(self, client: AsyncClient) -> None:
        """Other end: the caller's OWN memory, linked to a foreign entity.

        The memory is the attacker's, so the named end passes. Only the entity
        belongs elsewhere — this is the case the entity half of the predicate
        exists for, and the one a memory-side-only check would leak.
        """
        victim, attacker = _tenant(), _tenant()
        attacker_memory = await _memory(client, attacker)
        victim_entity = await _entity(client, victim)
        await _force_link(attacker_memory, victim_entity)

        body = await self._read(client, attacker_memory, attacker)

        assert body.get(attacker_memory, []) == [], (
            "a foreign entity's UUID was disclosed through the caller's own memory"
        )

    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        tenant = _tenant()
        memory_id = await _memory(client, tenant)

        resp = await client.post(f"{PREFIX}/memories/entity-links", json={"memory_ids": [memory_id]})

        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.text

    async def test_own_links_are_returned(self, client: AsyncClient) -> None:
        """The supported call still works."""
        tenant = _tenant()
        memory_id = await _memory(client, tenant)
        first, second = await _entity(client, tenant), await _entity(client, tenant)
        await _force_link(memory_id, first, "subject")
        await _force_link(memory_id, second, "object")

        body = await self._read(client, memory_id, tenant)

        assert {link["entity_id"]: link["role"] for link in body[memory_id]} == {
            first: "subject",
            second: "object",
        }


class TestMemoryIdsByEntityIdsRead:
    """``POST /entities/memory-ids-by-entity-ids``."""

    async def _read(self, client: AsyncClient, entity_id: str, tenant_id: str) -> list[dict]:
        resp = await client.post(
            f"{PREFIX}/entities/memory-ids-by-entity-ids",
            json={"entity_ids": [entity_id], "tenant_id": tenant_id},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def test_a_foreign_entity_yields_no_memory_ids(self, client: AsyncClient) -> None:
        """Named end: someone else's entity returns none of its memory ids."""
        victim, attacker = _tenant(), _tenant()
        victim_memory = await _memory(client, victim)
        victim_entity = await _entity(client, victim)
        await _force_link(victim_memory, victim_entity)

        assert await self._read(client, victim_entity, attacker) == [], (
            "another tenant's memory ids were returned"
        )

    async def test_a_foreign_memory_id_is_not_disclosed(self, client: AsyncClient) -> None:
        """Other end: the caller's OWN entity, linked to a foreign memory.

        This route's payload IS memory ids and its caller fetches them next, so
        without the memory-side check it hands out ids to look up in a tenant the
        caller cannot read.
        """
        victim, attacker = _tenant(), _tenant()
        attacker_entity = await _entity(client, attacker)
        victim_memory = await _memory(client, victim)
        await _force_link(victim_memory, attacker_entity)

        assert await self._read(client, attacker_entity, attacker) == [], (
            "a foreign memory id was disclosed through the caller's own entity"
        )

    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"{PREFIX}/entities/memory-ids-by-entity-ids",
            json={"entity_ids": [str(uuid.uuid4())]},
        )
        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.text

    async def test_own_links_are_returned(self, client: AsyncClient) -> None:
        tenant = _tenant()
        memory_id = await _memory(client, tenant)
        entity_id = await _entity(client, tenant)
        await _force_link(memory_id, entity_id, "subject")

        assert await self._read(client, entity_id, tenant) == [
            {"memory_id": memory_id, "entity_id": entity_id, "role": "subject"}
        ]


class TestCountMemoriesPerEntityRead:
    """``POST /entities/count-memories``.

    The client has always sent ``tenant_id`` in this body; the route read only
    ``entity_ids`` and dropped it, so the count spanned every tenant sharing the
    entity. Nothing changed on the caller's side.
    """

    async def _read(self, client: AsyncClient, entity_id: str, tenant_id: str) -> dict:
        resp = await client.post(
            f"{PREFIX}/entities/count-memories",
            json={"entity_ids": [entity_id], "tenant_id": tenant_id},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def test_a_foreign_entity_counts_nothing(self, client: AsyncClient) -> None:
        """Named end: someone else's entity has no count for this caller."""
        victim, attacker = _tenant(), _tenant()
        victim_memory = await _memory(client, victim)
        victim_entity = await _entity(client, victim)
        await _force_link(victim_memory, victim_entity)

        assert await self._read(client, victim_entity, attacker) == {}, (
            "another tenant's link count was returned"
        )

    async def test_foreign_memories_are_not_counted(self, client: AsyncClient) -> None:
        """Other end: the caller's own entity, plus links from foreign memories.

        Two of the three links belong to another tenant's memories. Only the
        caller's own may be counted — otherwise the number itself reports on rows
        the caller cannot read, which is what an entity shared across tenants
        would leak.
        """
        victim, attacker = _tenant(), _tenant()
        shared_entity = await _entity(client, attacker)
        await _force_link(await _memory(client, attacker), shared_entity)
        await _force_link(await _memory(client, victim), shared_entity)
        await _force_link(await _memory(client, victim), shared_entity)

        assert await self._read(client, shared_entity, attacker) == {shared_entity: 1}, (
            "the count included memories belonging to another tenant"
        )

    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"{PREFIX}/entities/count-memories", json={"entity_ids": [str(uuid.uuid4())]}
        )
        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.text
