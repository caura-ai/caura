"""``POST /entities/links/bulk`` must be told which tenant it links for.

The handler accepted only caller-supplied ``memory_id``, ``entity_id`` and
``role``. It read no tenant, and ``memory_entity_links``' foreign keys prove
only that the two UUIDs exist — not that they belong to the same tenant, nor to
the caller. On a service that authenticates no request (GHSA-wgvw-28pq-jc36),
published by docker-compose on 0.0.0.0:8002, that made knowing two UUIDs
sufficient to create a graph edge across a tenant boundary.

The join table has no ``tenant_id``, so the link row carries no predicate of its
own; both parents do (``Memory.tenant_id``, ``Entity.tenant_id``), which is what
makes an existence check on the two ends sufficient and a schema change
unnecessary.

**Both ends are load-bearing and neither implies the other**, so each has its own
case: skip the entity check and a caller staples a foreign entity
onto their own memory, pulling another tenant's graph node into their namespace;
skip the memory check and they staple their own entity onto a foreign memory.
Reverting one predicate fails only that direction's tests.

The refusals are deliberately indistinguishable from "that row does not exist":
``error="fk_violation"``. Distinguishing them would answer as an existence
oracle for two id spaces, which is what
``test_a_foreign_pair_is_indistinguishable_from_a_ghost`` pins.

The single-link ``POST /entities/links`` had the same guard and no production
caller, and was deleted (L-205).
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
    9,186 rows behind. This suite now defaults to a database of its own, in CI
    and locally alike, so that sweep no longer reaches these rows either way and
    the prefix is a convention this module keeps rather than a live guard. Point
    ``DATABASE_URL`` back at the root suite's database and the prefix is load-
    bearing again — which is the reason to keep minting it.

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
    """Read the raw join table, tenant-blind, to assert what actually persisted.

    Reads ``memory_entity_links`` with SQL rather than through
    ``POST /memories/entity-links``, which it used to use. That route is now
    tenant-scoped and returns only rows whose memory AND entity are both in the
    given tenant — precisely the rows these tests are checking for the ABSENCE
    of. Asking it would answer "no links" for a cross-tenant row that exists,
    so every assertion below would hold with the bug reintroduced.

    ``client`` is unused but kept in the signature so the call sites read the
    same and the helper stays swappable; the raw read is the point.
    """
    from sqlalchemy import text

    from core_storage_api.services.postgres_service import get_session

    async with get_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT entity_id, role FROM memory_entity_links "
                    "WHERE memory_id = CAST(:mid AS uuid) ORDER BY entity_id"
                ),
                {"mid": memory_id},
            )
        ).all()
    return [{"entity_id": str(entity_id), "role": role} for entity_id, role in rows]


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
