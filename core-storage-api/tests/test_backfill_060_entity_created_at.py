"""O17: the backfill dates entities from their earliest linked memory (H-05).

The nightly duplicate merge keeps the first seen of two compatible unqualified
names, by ``entities.created_at`` (caura PR #1876). Migration 060 added the
column with ``DEFAULT now()``, so every entity written before it reads the
migration's time, and between those the merge falls back to the longer name.
``core_storage_api.scripts.backfill_060_entity_created_at`` dates each one from
the earliest memory linked to it (owner decision 2026-10-06).

Each run here is scoped to its own tenant, as an operator can scope one, so the
backfill never touches another test's rows.
"""

from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from common.models import Entity, MemoryEntityLink
from core_storage_api.database.init import get_engine
from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("_ensure_schema")]

_svc = PostgresService()

# What every entity written before migration 060 reads.
MIGRATED = datetime(2026, 10, 6, tzinfo=UTC)
JAN = datetime(2025, 1, 1, tzinfo=UTC)
JUN = datetime(2025, 6, 1, tzinfo=UTC)


def _backfill():
    """The script, imported when a test runs rather than at collection, so a tree
    without it fails these tests instead of stopping the whole suite."""
    return importlib.import_module("core_storage_api.scripts.backfill_060_entity_created_at")


def _tenant() -> str:
    return f"o17-{uuid.uuid4().hex[:8]}"


async def _set(table: str, row_id: uuid.UUID, column: str, value: datetime) -> None:
    async with get_engine().begin() as conn:
        await conn.execute(
            text(f"UPDATE {table} SET {column} = :v WHERE id = :id"), {"v": value, "id": row_id}
        )


async def _entity(tenant: str, name: str, *, created_at: datetime = MIGRATED) -> uuid.UUID:
    row = await _svc.entity_add(
        {
            "tenant_id": tenant,
            "entity_type": "organization",
            "canonical_name": f"{name} {uuid.uuid4().hex[:6]}",
        }
    )
    await _set("entities", row.id, "created_at", created_at)
    return row.id


async def _memory(tenant: str, created_at: datetime, *, deleted: bool = False) -> uuid.UUID:
    row = await _svc.memory_add(
        {
            "tenant_id": tenant,
            "agent_id": "o17-agent",
            "content": f"o17 {uuid.uuid4()}",
            "memory_type": "fact",
            "status": "active",
            "visibility": "scope_team",
        }
    )
    await _set("memories", row.id, "created_at", created_at)
    if deleted:
        await _set("memories", row.id, "deleted_at", created_at)
    return row.id


async def _link(memory_id: uuid.UUID, entity_id: uuid.UUID) -> None:
    async with get_session() as session:
        session.add(MemoryEntityLink(memory_id=memory_id, entity_id=entity_id, role="mention"))


async def _created_at(entity_id: uuid.UUID) -> datetime:
    async with get_engine().connect() as conn:
        return (await conn.execute(select(Entity.created_at).where(Entity.id == entity_id))).scalar_one()


async def _run(tenant: str, *, dry_run: bool = False, after_id: uuid.UUID | None = None):
    return await _backfill()._run(tenant_id=tenant, batch=2, dry_run=dry_run, after_id=after_id)


async def test_an_entity_takes_its_earliest_linked_memorys_time():
    tenant = _tenant()
    entity = await _entity(tenant, "Acme")
    for when in (JUN, JAN):
        await _link(await _memory(tenant, when), entity)

    run = await _run(tenant)

    assert await _created_at(entity) == JAN
    assert run.dated == 1


async def test_a_time_only_ever_moves_earlier():
    """An entity dated before its memories keeps its time, and one no memory
    links keeps the one it has."""
    tenant = _tenant()
    early = await _entity(tenant, "Early", created_at=datetime(2024, 1, 1, tzinfo=UTC))
    await _link(await _memory(tenant, JAN), early)
    unlinked = await _entity(tenant, "Unlinked")

    run = await _run(tenant)

    assert await _created_at(early) == datetime(2024, 1, 1, tzinfo=UTC)
    assert await _created_at(unlinked) == MIGRATED
    assert run.dated == 0


async def test_only_a_memory_of_the_entitys_own_tenant_dates_it():
    tenant = _tenant()
    entity = await _entity(tenant, "Acme")
    await _link(await _memory(_tenant(), JAN), entity)
    await _link(await _memory(tenant, JUN), entity)

    await _run(tenant)

    assert await _created_at(entity) == JUN


async def test_a_memory_deleted_since_still_dates_it():
    """It still shows when the entity was first seen."""
    tenant = _tenant()
    entity = await _entity(tenant, "Acme")
    await _link(await _memory(tenant, JAN, deleted=True), entity)

    await _run(tenant)

    assert await _created_at(entity) == JAN


async def test_a_dry_run_counts_and_changes_nothing():
    tenant = _tenant()
    entities = [await _entity(tenant, f"Acme {i}") for i in range(3)]
    for entity in entities:
        await _link(await _memory(tenant, JAN), entity)

    run = await _run(tenant, dry_run=True)

    assert (run.entities, run.dated) == (3, 3)
    assert [await _created_at(e) for e in entities] == [MIGRATED] * 3


async def test_a_run_scoped_to_one_tenant_leaves_the_others():
    tenant, other = _tenant(), _tenant()
    in_other = await _entity(other, "Acme")
    await _link(await _memory(other, JAN), in_other)

    await _run(tenant)

    assert await _created_at(in_other) == MIGRATED


async def test_a_second_run_finds_nothing_to_date():
    tenant = _tenant()
    entity = await _entity(tenant, "Acme")
    await _link(await _memory(tenant, JAN), entity)
    await _run(tenant)

    again = await _run(tenant)

    assert (again.entities, again.dated) == (1, 0)


async def test_a_run_resumes_after_the_id_it_is_given():
    tenant = _tenant()
    first, second = sorted([await _entity(tenant, "Acme"), await _entity(tenant, "Globex")])
    for entity in (first, second):
        await _link(await _memory(tenant, JAN), entity)

    run = await _run(tenant, after_id=first)

    assert await _created_at(first) == MIGRATED
    assert await _created_at(second) == JAN
    assert run.last_id == second
