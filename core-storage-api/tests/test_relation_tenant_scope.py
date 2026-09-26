"""M-64 — a relation's ENDPOINTS must belong to the tenant, not just the edge.

``Relation`` has a ``tenant_id``, which is what made this easy to miss: the edge
carried a predicate, so the queries looked scoped. But the column describes the
edge, not the two entities it names, and the FKs only require those rows to exist
in SOME tenant. Three surfaces, and they fail independently:

* ``relation_add`` accepted any entity UUID, so a write key for tenant T could
  point an edge at a victim entity in tenant U.
* ``relation_get_outgoing`` joined the TARGET entity on id alone, so reading that
  edge back handed over U's ``canonical_name`` and ``attributes``.
* ``memory_get_detail`` joined the linked entity on id alone — the same
  disclosure through ``memory_entity_links``, which is NOT part of the reported
  finding and was found by auditing every ``join(Entity`` in the service.

The write guard stops new straddling rows. It does nothing for the ones already
there, which is why each reader is fixed and tested separately: reverting either
predicate leaves a live disclosure that the other test still passes over.

Straddling rows are created here with raw SQL on purpose. Both write paths refuse
them now, so going through the API would test the guard instead of the reader and
every assertion below would hold with the bug reintroduced — the failure mode
``test_memory_entity_links_tenant_scope._links`` documents for the same reason.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from core_storage_api.services.postgres_service import get_session
from tests.test_integration import PREFIX, _memory_payload

pytestmark = [pytest.mark.asyncio]


def _tenant() -> str:
    """See ``test_memory_entity_links_tenant_scope._tenant`` — same prefix contract."""
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


async def _entity(client: AsyncClient, tenant_id: str, name: str | None = None) -> str:
    resp = await client.post(
        f"{PREFIX}/entities",
        json={
            "tenant_id": tenant_id,
            "entity_type": "person",
            "canonical_name": name or f"RelScope-{uuid.uuid4().hex[:8]}",
            "attributes": {"secret": "victim-only"},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _memory(client: AsyncClient, tenant_id: str) -> str:
    fleet_id = f"test-fleet-{uuid.uuid4().hex[:8]}"
    resp = await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant_id, fleet_id))
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _straddling_relation(tenant_id: str, from_id: str, to_id: str) -> None:
    """Insert the row the write path now refuses, as history already holds it."""
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO relations (id, tenant_id, from_entity_id, relation_type, "
                "to_entity_id, weight) VALUES (gen_random_uuid(), :t, CAST(:f AS uuid), "
                "'knows', CAST(:o AS uuid), 0.5)"
            ),
            {"t": tenant_id, "f": from_id, "o": to_id},
        )
        await session.commit()


async def _straddling_link(memory_id: str, entity_id: str) -> None:
    """Same, for ``memory_entity_links`` — refused since #1085/#1124."""
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO memory_entity_links (memory_id, entity_id, role) "
                "VALUES (CAST(:m AS uuid), CAST(:e AS uuid), 'subject')"
            ),
            {"m": memory_id, "e": entity_id},
        )
        await session.commit()


class TestRelationWriteTenantScope:
    async def test_a_foreign_to_entity_is_refused(self, client: AsyncClient) -> None:
        attacker, victim = _tenant(), _tenant()
        mine = await _entity(client, attacker)
        theirs = await _entity(client, victim)

        resp = await client.post(
            f"{PREFIX}/entities/relations",
            json={
                "tenant_id": attacker,
                "from_entity_id": mine,
                "relation_type": "knows",
                "to_entity_id": theirs,
            },
        )
        assert resp.status_code == 409, resp.text

    async def test_a_foreign_from_entity_is_refused(self, client: AsyncClient) -> None:
        """The other end, separately — the two failures are independent.

        Guarding only ``to_entity_id`` still lets an attacker hang an edge off a
        victim's entity, which ``relation_get_outgoing`` then serves to anyone
        who can name it.
        """
        attacker, victim = _tenant(), _tenant()
        mine = await _entity(client, attacker)
        theirs = await _entity(client, victim)

        resp = await client.post(
            f"{PREFIX}/entities/relations",
            json={
                "tenant_id": attacker,
                "from_entity_id": theirs,
                "relation_type": "knows",
                "to_entity_id": mine,
            },
        )
        assert resp.status_code == 409, resp.text

    async def test_own_relation_is_created(self, client: AsyncClient) -> None:
        """OVER-REFUSAL GUARD. A guard that refused everything passes every test above."""
        tenant = _tenant()
        a, b = await _entity(client, tenant), await _entity(client, tenant)

        resp = await client.post(
            f"{PREFIX}/entities/relations",
            json={
                "tenant_id": tenant,
                "from_entity_id": a,
                "relation_type": "knows",
                "to_entity_id": b,
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["from_entity_id"] == a

    async def test_a_foreign_id_is_indistinguishable_from_a_missing_one(self, client: AsyncClient) -> None:
        """No existence oracle. This service authenticates nothing.

        If "that entity is another tenant's" answered differently from "no such
        entity", the route would confirm the existence of any UUID a caller cared
        to guess — the reason ``_LINK_REJECTED`` is one message for both, per
        GHSA-wgvw-28pq-jc36.
        """
        attacker, victim = _tenant(), _tenant()
        mine = await _entity(client, attacker)
        theirs = await _entity(client, victim)
        nonexistent = str(uuid.uuid4())

        foreign = await client.post(
            f"{PREFIX}/entities/relations",
            json={
                "tenant_id": attacker,
                "from_entity_id": mine,
                "relation_type": "knows",
                "to_entity_id": theirs,
            },
        )
        missing = await client.post(
            f"{PREFIX}/entities/relations",
            json={
                "tenant_id": attacker,
                "from_entity_id": mine,
                "relation_type": "knows",
                "to_entity_id": nonexistent,
            },
        )
        assert foreign.status_code == missing.status_code
        assert foreign.json() == missing.json(), (
            "a foreign entity answers differently from a missing one; the route "
            "is an existence oracle over the whole entity id space"
        )


class TestRelationReadTenantScope:
    async def test_a_straddling_relation_does_not_disclose_its_target(self, client: AsyncClient) -> None:
        """The row the write guard cannot reach: one that already exists."""
        attacker, victim = _tenant(), _tenant()
        mine = await _entity(client, attacker)
        theirs = await _entity(client, victim, name="Victim-Confidential")
        await _straddling_relation(attacker, mine, theirs)

        resp = await client.get(f"{PREFIX}/entities/{mine}/relations", params={"tenant_id": attacker})
        assert resp.status_code == 200, resp.text
        assert resp.json() == [], f"another tenant's entity was served as a relation target: {resp.text}"

    async def test_own_relations_are_still_returned(self, client: AsyncClient) -> None:
        """OVER-REFUSAL GUARD on the join predicate."""
        tenant = _tenant()
        a, b = await _entity(client, tenant), await _entity(client, tenant)
        created = await client.post(
            f"{PREFIX}/entities/relations",
            json={
                "tenant_id": tenant,
                "from_entity_id": a,
                "relation_type": "knows",
                "to_entity_id": b,
            },
        )
        assert created.status_code == 200, created.text

        resp = await client.get(f"{PREFIX}/entities/{a}/relations", params={"tenant_id": tenant})
        assert resp.status_code == 200, resp.text
        targets = [row["target"]["id"] for row in resp.json()]
        assert targets == [b], f"a same-tenant relation was dropped by the join: {resp.text}"


class TestMemoryDetailEntityTenantScope:
    async def test_a_straddling_link_does_not_disclose_the_entity(self, client: AsyncClient) -> None:
        """Surface #3, absent from the finding's text.

        The memory is tenant-checked; the entity joined onto it was not. The link
        row itself has no tenant column, so nothing else stood between a
        historical straddling row and the victim's name.
        """
        attacker, victim = _tenant(), _tenant()
        memory_id = await _memory(client, attacker)
        theirs = await _entity(client, victim, name="Victim-Confidential")
        await _straddling_link(memory_id, theirs)

        resp = await client.get(f"{PREFIX}/memories/{memory_id}/detail", params={"tenant_id": attacker})
        assert resp.status_code == 200, resp.text
        links = resp.json()["entity_links"]
        assert len(links) == 1, links
        # The link is still reported — it is this tenant's row and hiding it
        # would misrepresent the memory. Only the foreign entity's fields go.
        assert links[0]["entity_id"] == theirs
        assert links[0]["role"] == "subject"
        assert "canonical_name" not in links[0], f"the victim's entity name leaked: {links[0]}"
        assert "attributes" not in links[0], f"the victim's attributes leaked: {links[0]}"

    async def test_own_link_still_carries_the_entity_name(self, client: AsyncClient) -> None:
        """OVER-REFUSAL GUARD. The outer join must still populate same-tenant rows."""
        tenant = _tenant()
        memory_id = await _memory(client, tenant)
        mine = await _entity(client, tenant, name="Mine-Visible")
        await _straddling_link(memory_id, mine)

        resp = await client.get(f"{PREFIX}/memories/{memory_id}/detail", params={"tenant_id": tenant})
        assert resp.status_code == 200, resp.text
        links = resp.json()["entity_links"]
        assert len(links) == 1, links
        assert links[0]["canonical_name"] == "Mine-Visible", (
            f"a same-tenant entity lost its fields to the new predicate: {links[0]}"
        )
