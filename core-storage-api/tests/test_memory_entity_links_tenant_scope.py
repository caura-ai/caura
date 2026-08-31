"""``PATCH /memories/{memory_id}/entities`` must be told which tenant it links for.

The write form of GHSA-wgvw-28pq-jc36 on the memory→entity join table. The route
validated the shape of ``entity_links`` carefully and read no tenant at all, so
``memory_add_entity_links`` upserted against a bare ``memory_id`` with bare
``entity_id`` values.

``memory_entity_links`` has no ``tenant_id``, so the join row carries no
predicate of its own — but both parents do (``Memory.tenant_id``,
``Entity.tenant_id``), which is why an existence check on the two ends closes
this and no schema change is involved.

**The two ends fail independently, so they are tested independently.** With only
the memory checked, a caller staples a foreign entity onto their own memory and
pulls another tenant's graph node into their namespace; with only the entities
checked, they staple their own entity onto a foreign memory. A test that covers
one direction passes while half the hole stands open — hence
``test_foreign_memory_is_not_linked`` and ``test_foreign_entity_is_not_linked``
as separate cases, each of which fails on its own if its half of the predicate
is reverted.
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
    (and reaches this table through the ``memories`` subquery), so a tenant
    minted with any other prefix is never reclaimed — which is how #858 left
    9,186 rows behind. Locally this suite's default ``DATABASE_URL`` points at
    the same database that sweep runs against, so the prefix is what gets these
    rows collected; in CI the storage suite is given a database of its own and
    nothing sweeps either way.

    Local to this module rather than ``tests.conftest.new_tenant_id``, which is
    the root suite's fixture file and not importable from this one. Same prefix
    contract, deliberately.
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
            "canonical_name": f"LinkScope-{uuid.uuid4().hex[:8]}",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _links(client: AsyncClient, memory_id: str) -> list[dict]:
    """Read the links back regardless of tenant, to assert what actually persisted.

    Deliberately goes through ``POST /memories/entity-links``, which is still
    unscoped (allowlisted ``id-addressed-read``) and so answers for any tenant —
    that is what makes it a usable oracle here. When that route gains a required
    tenant, this helper needs one too.
    """
    resp = await client.post(f"{PREFIX}/memories/entity-links", json={"memory_ids": [memory_id]})
    assert resp.status_code == 200, resp.text
    return resp.json().get(memory_id, [])


class TestMemoryEntityLinkTenantScope:
    async def test_foreign_memory_is_not_linked(self, client: AsyncClient) -> None:
        """Memory side: the attacker's own entity, hung off someone else's memory."""
        victim, attacker = _tenant(), _tenant()
        victim_memory = await _memory(client, victim)
        attacker_entity = await _entity(client, attacker)

        resp = await client.patch(
            f"{PREFIX}/memories/{victim_memory}/entities",
            json={
                "tenant_id": attacker,
                "entity_links": [{"entity_id": attacker_entity, "role": "subject"}],
            },
        )

        assert resp.status_code == 404, resp.text
        assert await _links(client, victim_memory) == [], (
            "the attacker's entity was linked to a foreign memory"
        )

    async def test_foreign_entity_is_not_linked(self, client: AsyncClient) -> None:
        """Entity side: someone else's entity, hung off the attacker's own memory.

        Nothing here is a foreign *memory* — the row being written belongs to the
        caller. Only the entity end is out of tenant, so this case is what the
        entity half of the predicate exists for.
        """
        victim, attacker = _tenant(), _tenant()
        attacker_memory = await _memory(client, attacker)
        victim_entity = await _entity(client, victim)

        resp = await client.patch(
            f"{PREFIX}/memories/{attacker_memory}/entities",
            json={
                "tenant_id": attacker,
                "entity_links": [{"entity_id": victim_entity, "role": "subject"}],
            },
        )

        assert resp.status_code == 404, resp.text
        assert await _links(client, attacker_memory) == [], (
            "a foreign entity was pulled into the caller's graph"
        )

    async def test_one_foreign_entity_voids_the_whole_request(self, client: AsyncClient) -> None:
        """A mixed batch writes nothing — not "the legitimate ones, minus the bad".

        Partial application would still land the foreign edge's siblings and
        report success, leaving the caller unable to tell which of their links
        exist. All-or-nothing keeps the failure legible.
        """
        victim, attacker = _tenant(), _tenant()
        attacker_memory = await _memory(client, attacker)
        own_entity = await _entity(client, attacker)
        victim_entity = await _entity(client, victim)

        resp = await client.patch(
            f"{PREFIX}/memories/{attacker_memory}/entities",
            json={
                "tenant_id": attacker,
                "entity_links": [
                    {"entity_id": own_entity, "role": "subject"},
                    {"entity_id": victim_entity, "role": "object"},
                ],
            },
        )

        assert resp.status_code == 404, resp.text
        assert await _links(client, attacker_memory) == []

    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        """No tenant used to mean "link by primary key"; it is now a 422."""
        tenant = _tenant()
        memory_id = await _memory(client, tenant)
        entity_id = await _entity(client, tenant)

        resp = await client.patch(
            f"{PREFIX}/memories/{memory_id}/entities",
            json={"entity_links": [{"entity_id": entity_id, "role": "subject"}]},
        )

        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.text
        assert await _links(client, memory_id) == []

    async def test_own_links_are_created(self, client: AsyncClient) -> None:
        """The supported call still works, for both ends in one tenant."""
        tenant = _tenant()
        memory_id = await _memory(client, tenant)
        first, second = await _entity(client, tenant), await _entity(client, tenant)

        resp = await client.patch(
            f"{PREFIX}/memories/{memory_id}/entities",
            json={
                "tenant_id": tenant,
                "entity_links": [
                    {"entity_id": first, "role": "subject"},
                    {"entity_id": second, "role": "mentioned"},
                ],
            },
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True
        assert {link["entity_id"]: link["role"] for link in await _links(client, memory_id)} == {
            first: "subject",
            second: "mentioned",
        }

    async def test_a_foreign_id_is_indistinguishable_from_a_missing_one(self, client: AsyncClient) -> None:
        """Both ends: naming a real row you don't own answers as "not there".

        Same status and same detail for a UUID that exists elsewhere and one that
        exists nowhere, on each end in turn — otherwise the route is an existence
        oracle for two id spaces, on a service that authenticates no request.
        """
        victim, attacker = _tenant(), _tenant()
        attacker_memory = await _memory(client, attacker)
        attacker_entity = await _entity(client, attacker)

        foreign_memory = await _memory(client, victim)
        absent_memory = str(uuid.uuid4())
        memory_answers = [
            await client.patch(
                f"{PREFIX}/memories/{mid}/entities",
                json={
                    "tenant_id": attacker,
                    "entity_links": [{"entity_id": attacker_entity, "role": "subject"}],
                },
            )
            for mid in (foreign_memory, absent_memory)
        ]
        assert [r.status_code for r in memory_answers] == [404, 404]
        # Both details name the memory id the caller asked about, and nothing else.
        assert [r.json()["detail"] for r in memory_answers] == [
            f"Memory {foreign_memory} not found",
            f"Memory {absent_memory} not found",
        ]

        foreign_entity = await _entity(client, victim)
        absent_entity = str(uuid.uuid4())
        entity_answers = [
            await client.patch(
                f"{PREFIX}/memories/{attacker_memory}/entities",
                json={"tenant_id": attacker, "entity_links": [{"entity_id": eid, "role": "subject"}]},
            )
            for eid in (foreign_entity, absent_entity)
        ]
        assert [r.status_code for r in entity_answers] == [404, 404]
        # The detail names the memory, not the entity that was actually refused,
        # so the two entity cases are not merely equal to each other — they
        # disclose nothing about the entity id space at all.
        detail = f"Memory {attacker_memory} not found"
        assert [r.json()["detail"] for r in entity_answers] == [detail, detail]
