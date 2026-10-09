"""A batch of entity links is one transaction (audit 2026-10-01, B33, L-196).

``entity_bulk_upsert_links`` opened a writer session, and so committed a
transaction, for every link in turn: up to 500 commits for the 500 links one
inline write can send, each waiting for a WAL flush. It now writes the batch as
one statement in one transaction, and an endpoint that vanished between the
ownership read and the insert costs only its own link.

The entity half of the finding stays per item (Eldad, 2026-10-09): an entity
update locks its row, and one transaction would hold every such lock for the
whole batch.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

import pytest
from httpx import AsyncClient

from core_storage_api.services import postgres_service
from core_storage_api.services.postgres_service import PostgresService
from tests.test_entity_link_routes_tenant_scope import _entity, _links, _memory, _tenant

pytestmark = [pytest.mark.asyncio]


@pytest.fixture
def sessions(monkeypatch) -> list[int]:
    """One entry per writer session, so per transaction, the service opens."""
    opened: list[int] = []
    real = postgres_service.get_session

    @asynccontextmanager
    async def counted():
        opened.append(1)
        async with real() as session:
            yield session

    monkeypatch.setattr(postgres_service, "get_session", counted)
    return opened


async def test_l196_a_link_batch_is_one_transaction(client: AsyncClient, sessions) -> None:
    tenant = _tenant()
    memory = await _memory(client, tenant)
    first, second = await _entity(client, tenant), await _entity(client, tenant)
    items = [
        {"input_idx": 0, "memory_id": memory, "entity_id": first, "role": "subject"},
        {"input_idx": 1, "memory_id": memory, "entity_id": second, "role": "object"},
        # The same pair again: it lands once and keeps the first role.
        {"input_idx": 2, "memory_id": memory, "entity_id": first, "role": "object"},
    ]
    sessions.clear()

    results = await PostgresService().entity_bulk_upsert_links(tenant, items)

    assert len(sessions) == 1
    assert [(r["input_idx"], r["created"], r["role"]) for r in results] == [
        (0, True, "subject"),
        (1, True, "object"),
        (2, False, "subject"),
    ]
    stored = sorted([(first, "subject"), (second, "object")])
    assert await _links(client, memory) == [{"entity_id": e, "role": r} for e, r in stored]


async def test_l196_a_vanished_endpoint_costs_only_its_own_link(
    client: AsyncClient, sessions, monkeypatch
) -> None:
    tenant = _tenant()
    memory = await _memory(client, tenant)
    entity = await _entity(client, tenant)
    ghost = str(uuid.uuid4())
    real_owned = PostgresService._owned_link_endpoints

    async def owned_then_gone(self, session, tenant_id, memory_ids, entity_ids):
        # The ownership read saw the ghost entity; it is gone by the insert.
        memories, entities = await real_owned(self, session, tenant_id, memory_ids, entity_ids)
        return memories, entities | {uuid.UUID(ghost)}

    monkeypatch.setattr(PostgresService, "_owned_link_endpoints", owned_then_gone)
    items = [
        {"input_idx": 0, "memory_id": memory, "entity_id": ghost, "role": "subject"},
        {"input_idx": 1, "memory_id": memory, "entity_id": entity, "role": "object"},
    ]
    sessions.clear()

    results = await PostgresService().entity_bulk_upsert_links(tenant, items)

    assert len(sessions) == 1
    assert (results[0]["created"], results[0].get("error")) == (False, "fk_violation")
    assert (results[1]["created"], results[1].get("error")) == (True, None)
    assert await _links(client, memory) == [{"entity_id": entity, "role": "object"}]
