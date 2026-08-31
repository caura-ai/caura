"""``POST /entities/links`` and ``/links/bulk`` must be told which tenant they link for.

Both handlers accepted only caller-supplied ``memory_id``, ``entity_id`` and
``role``. Neither read a tenant, and ``memory_entity_links``' foreign keys prove
only that the two UUIDs exist — not that they belong to the same tenant, nor to
the caller. On a service that authenticates no request (GHSA-wgvw-28pq-jc36),
published by docker-compose on 0.0.0.0:8002, that made knowing two UUIDs
sufficient to create a graph edge across a tenant boundary.

The join table has no ``tenant_id``, so the link row carries no predicate of its
own; both parents do (``Memory.tenant_id``, ``Entity.tenant_id``), which is what
makes an existence check on the two ends sufficient and a schema change
unnecessary.

**Both ends are load-bearing and neither implies the other**, so each has its own
case on each route: skip the entity check and a caller staples a foreign entity
onto their own memory, pulling another tenant's graph node into their namespace;
skip the memory check and they staple their own entity onto a foreign memory.
Reverting one predicate fails only that direction's tests.

The refusals are deliberately indistinguishable from "that row does not exist" —
409 with a fixed message on the single route, ``error="fk_violation"`` on the
bulk route. Distinguishing them would answer as an existence oracle for two id
spaces, which is what ``test_a_foreign_pair_is_indistinguishable_from_a_ghost``
pins on each route.
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
            "canonical_name": f"LinkRoute-{uuid.uuid4().hex[:8]}",
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


class TestSingleLinkRouteTenantScope:
    """``POST /entities/links``."""

    async def test_foreign_memory_is_not_linked(self, client: AsyncClient) -> None:
        """Memory side: the attacker's own entity, hung off someone else's memory."""
        victim, attacker = _tenant(), _tenant()
        victim_memory = await _memory(client, victim)
        attacker_entity = await _entity(client, attacker)

        resp = await client.post(
            f"{PREFIX}/entities/links",
            json={
                "tenant_id": attacker,
                "memory_id": victim_memory,
                "entity_id": attacker_entity,
                "role": "subject",
            },
        )

        assert resp.status_code == 409, resp.text
        assert await _links(client, victim_memory) == [], "an edge was written onto a foreign memory"

    async def test_foreign_entity_is_not_linked(self, client: AsyncClient) -> None:
        """Entity side: someone else's entity, hung off the attacker's own memory.

        The memory here belongs to the caller, so only the entity end is out of
        tenant — this is the case the entity half of the predicate exists for.
        """
        victim, attacker = _tenant(), _tenant()
        attacker_memory = await _memory(client, attacker)
        victim_entity = await _entity(client, victim)

        resp = await client.post(
            f"{PREFIX}/entities/links",
            json={
                "tenant_id": attacker,
                "memory_id": attacker_memory,
                "entity_id": victim_entity,
                "role": "subject",
            },
        )

        assert resp.status_code == 409, resp.text
        assert await _links(client, attacker_memory) == [], (
            "a foreign entity was pulled into the caller's graph"
        )

    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        """No tenant used to mean "link by primary key"; it is now a 422."""
        tenant = _tenant()
        memory_id = await _memory(client, tenant)
        entity_id = await _entity(client, tenant)

        resp = await client.post(
            f"{PREFIX}/entities/links",
            json={"memory_id": memory_id, "entity_id": entity_id, "role": "subject"},
        )

        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.text
        assert await _links(client, memory_id) == []

    async def test_own_link_is_created(self, client: AsyncClient) -> None:
        """The supported call still works, and ``tenant_id`` does not reach the row.

        ``MemoryEntityLink`` has no ``tenant_id`` column — the join table being
        tenantless is the whole difficulty — so the route must consume the field
        rather than pass it through to ``MemoryEntityLink(**data)``.
        """
        tenant = _tenant()
        memory_id = await _memory(client, tenant)
        entity_id = await _entity(client, tenant)

        resp = await client.post(
            f"{PREFIX}/entities/links",
            json={
                "tenant_id": tenant,
                "memory_id": memory_id,
                "entity_id": entity_id,
                "role": "subject",
            },
        )

        assert resp.status_code == 200, resp.text
        assert "tenant_id" not in resp.json()
        assert await _links(client, memory_id) == [{"entity_id": entity_id, "role": "subject"}]

    async def test_a_foreign_pair_is_indistinguishable_from_a_ghost(self, client: AsyncClient) -> None:
        """A real row you don't own answers exactly like a row that isn't there.

        Checked on each end in turn. If the tenant refusal carried its own status
        or message, the route would confirm which UUIDs exist in other tenants.
        """
        victim, attacker = _tenant(), _tenant()
        attacker_memory = await _memory(client, attacker)
        attacker_entity = await _entity(client, attacker)

        async def _post(memory_id: str, entity_id: str) -> tuple[int, str]:
            resp = await client.post(
                f"{PREFIX}/entities/links",
                json={
                    "tenant_id": attacker,
                    "memory_id": memory_id,
                    "entity_id": entity_id,
                    "role": "subject",
                },
            )
            return resp.status_code, resp.json()["detail"]

        foreign_entity = await _post(attacker_memory, await _entity(client, victim))
        ghost_entity = await _post(attacker_memory, str(uuid.uuid4()))
        foreign_memory = await _post(await _memory(client, victim), attacker_entity)
        ghost_memory = await _post(str(uuid.uuid4()), attacker_entity)

        assert foreign_entity == ghost_entity, "the entity end leaks which ids exist elsewhere"
        assert foreign_memory == ghost_memory, "the memory end leaks which ids exist elsewhere"
        # And all four are the same answer, so the caller cannot even tell which
        # END of the link was refused.
        assert foreign_entity == foreign_memory
        assert foreign_entity[0] == 409


class TestBulkLinkRouteTenantScope:
    """``POST /entities/links/bulk``."""

    async def _bulk(
        self, client: AsyncClient, tenant_id: str | None, items: list[dict]
    ) -> tuple[int, object]:
        body: dict = {"items": items}
        if tenant_id is not None:
            body["tenant_id"] = tenant_id
        resp = await client.post(f"{PREFIX}/entities/links/bulk", json=body)
        return resp.status_code, resp.json()

    async def test_foreign_memory_is_not_linked(self, client: AsyncClient) -> None:
        """Memory side, per item."""
        victim, attacker = _tenant(), _tenant()
        victim_memory = await _memory(client, victim)
        attacker_entity = await _entity(client, attacker)

        status, body = await self._bulk(
            client,
            attacker,
            [{"input_idx": 0, "memory_id": victim_memory, "entity_id": attacker_entity, "role": "subject"}],
        )

        assert status == 200, body
        assert body == [
            {
                "input_idx": 0,
                "memory_id": victim_memory,
                "entity_id": attacker_entity,
                "role": "subject",
                "created": False,
                "error": "fk_violation",
            }
        ]
        assert await _links(client, victim_memory) == [], "an edge was written onto a foreign memory"

    async def test_foreign_entity_is_not_linked(self, client: AsyncClient) -> None:
        """Entity side, per item — the memory belongs to the caller."""
        victim, attacker = _tenant(), _tenant()
        attacker_memory = await _memory(client, attacker)
        victim_entity = await _entity(client, victim)

        status, body = await self._bulk(
            client,
            attacker,
            [{"input_idx": 0, "memory_id": attacker_memory, "entity_id": victim_entity, "role": "subject"}],
        )

        assert status == 200, body
        assert body[0]["created"] is False  # type: ignore[index]
        assert body[0]["error"] == "fk_violation"  # type: ignore[index]
        assert await _links(client, attacker_memory) == [], (
            "a foreign entity was pulled into the caller's graph"
        )

    async def test_a_refused_item_does_not_discard_its_valid_siblings(self, client: AsyncClient) -> None:
        """Per-item isolation survives the tenant check.

        The pre-existing contract is that one bad item does not cost the batch
        (each insert has its own session). A tenant refusal has to behave the
        same way, or adding the check would have turned a partial failure into a
        total one for every caller.
        """
        victim, attacker = _tenant(), _tenant()
        memory_id = await _memory(client, attacker)
        own_entity = await _entity(client, attacker)
        victim_entity = await _entity(client, victim)

        status, body = await self._bulk(
            client,
            attacker,
            [
                {"input_idx": 0, "memory_id": memory_id, "entity_id": own_entity, "role": "subject"},
                {"input_idx": 1, "memory_id": memory_id, "entity_id": victim_entity, "role": "object"},
            ],
        )

        assert status == 200, body
        assert body[0] == {  # type: ignore[index]
            "input_idx": 0,
            "memory_id": memory_id,
            "entity_id": own_entity,
            "role": "subject",
            "created": True,
        }
        assert body[1]["error"] == "fk_violation"  # type: ignore[index]
        assert await _links(client, memory_id) == [{"entity_id": own_entity, "role": "subject"}]

    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        tenant = _tenant()
        memory_id = await _memory(client, tenant)
        entity_id = await _entity(client, tenant)

        status, body = await self._bulk(
            client,
            None,
            [{"input_idx": 0, "memory_id": memory_id, "entity_id": entity_id, "role": "subject"}],
        )

        assert status == 422, body
        assert "tenant_id" in str(body)
        assert await _links(client, memory_id) == []

    async def test_own_links_are_created(self, client: AsyncClient) -> None:
        tenant = _tenant()
        memory_id = await _memory(client, tenant)
        first, second = await _entity(client, tenant), await _entity(client, tenant)

        status, body = await self._bulk(
            client,
            tenant,
            [
                {"input_idx": 0, "memory_id": memory_id, "entity_id": first, "role": "subject"},
                {"input_idx": 1, "memory_id": memory_id, "entity_id": second, "role": "object"},
            ],
        )

        assert status == 200, body
        assert [item["created"] for item in body] == [True, True]  # type: ignore[union-attr]
        assert {link["entity_id"]: link["role"] for link in await _links(client, memory_id)} == {
            first: "subject",
            second: "object",
        }

    async def test_a_foreign_pair_is_indistinguishable_from_a_ghost(self, client: AsyncClient) -> None:
        """The batch route must not become a bulk existence oracle.

        This is the sharper version of the single-route case: a caller could
        submit 500 pairs and read off which of them exist in other tenants, in
        one request, if the refusals differed.
        """
        victim, attacker = _tenant(), _tenant()
        attacker_memory = await _memory(client, attacker)
        attacker_entity = await _entity(client, attacker)

        status, body = await self._bulk(
            client,
            attacker,
            [
                # 0: foreign entity. 1: entity that exists nowhere.
                {
                    "input_idx": 0,
                    "memory_id": attacker_memory,
                    "entity_id": await _entity(client, victim),
                    "role": "subject",
                },
                {
                    "input_idx": 1,
                    "memory_id": attacker_memory,
                    "entity_id": str(uuid.uuid4()),
                    "role": "subject",
                },
                # 2: foreign memory. 3: memory that exists nowhere.
                {
                    "input_idx": 2,
                    "memory_id": await _memory(client, victim),
                    "entity_id": attacker_entity,
                    "role": "subject",
                },
                {
                    "input_idx": 3,
                    "memory_id": str(uuid.uuid4()),
                    "entity_id": attacker_entity,
                    "role": "subject",
                },
            ],
        )

        assert status == 200, body
        # Every slot reports the same outcome; only the echoed ids differ, and
        # those are the caller's own input.
        outcomes = [{k: v for k, v in item.items() if k not in ("memory_id", "entity_id")} for item in body]  # type: ignore[union-attr]
        assert outcomes == [
            {"input_idx": idx, "role": "subject", "created": False, "error": "fk_violation"}
            for idx in range(4)
        ]
