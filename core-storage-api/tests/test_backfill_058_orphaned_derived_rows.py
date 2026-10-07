"""O12: the backfill soft-deletes live rows whose parent is gone, as the cascade would have.

Auto-chunk and atomic-fact children link to their parent only through
``metadata.parent_memory_id``. Since caura PR #1843 every delete takes the live
rows derived from what it deletes. Rows whose parent was deleted before that, or
never existed, are still live and still carry the deleted text.
``core_storage_api.scripts.backfill_058_orphaned_derived_rows`` finds them through
migration 058's index and deletes them the way the cascade does.

Each run here is scoped to its own tenant, as an operator can scope one, so the
backfill never touches another test's rows.
"""

from __future__ import annotations

import importlib
import uuid

import pytest
from sqlalchemy import select, text

from common.models import Memory
from core_storage_api.database.init import get_engine
from core_storage_api.services.postgres_service import PostgresService
from tests.conftest import plan_with_only_index

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("_ensure_schema")]

_svc = PostgresService()


def _backfill():
    """The script, imported when a test runs rather than at collection, so a tree
    without it fails these tests instead of stopping the whole suite."""
    return importlib.import_module("core_storage_api.scripts.backfill_058_orphaned_derived_rows")


def _tenant() -> str:
    return f"o12-{uuid.uuid4().hex[:8]}"


async def _add(tenant: str, label: str, *, parent: uuid.UUID | None = None) -> uuid.UUID:
    metadata = {"parent_memory_id": str(parent), "source": "auto_chunk"} if parent else {}
    row = await _svc.memory_add(
        {
            "tenant_id": tenant,
            "agent_id": "o12-agent",
            "content": f"{label} {uuid.uuid4()}",
            "memory_type": "fact",
            "status": "active",
            "visibility": "scope_team",
            "metadata_": metadata,
        }
    )
    return row.id


async def _delete_without_cascade(memory_id: uuid.UUID) -> None:
    """A delete made before caura PR #1843: the parent goes, its children stay."""
    async with get_engine().begin() as conn:
        await conn.execute(
            text("UPDATE memories SET deleted_at = now(), status = 'deleted' WHERE id = :id"),
            {"id": memory_id},
        )


async def _states(*ids: uuid.UUID) -> dict:
    """``{id: (status, deleted_at)}`` read from the table."""
    async with get_engine().connect() as conn:
        rows = await conn.execute(
            select(Memory.id, Memory.status, Memory.deleted_at).where(Memory.id.in_(ids))
        )
        return {r.id: (r.status, r.deleted_at) for r in rows}


async def _run(tenant: str, *, dry_run: bool = False, batch: int = 2):
    return await _backfill()._run(tenant_id=tenant, batch=batch, dry_run=dry_run, max_passes=10)


async def test_rows_whose_parent_is_gone_are_soft_deleted_as_the_cascade_would():
    tenant = _tenant()
    deleted_parent = await _add(tenant, "deleted parent")
    orphan = await _add(tenant, "orphan of a deleted parent", parent=deleted_parent)
    # Derived from the orphan: gone once the orphan is, on the same or the next pass.
    grandchild = await _add(tenant, "fact of the orphan", parent=orphan)
    never_written = await _add(tenant, "orphan of a missing parent", parent=uuid.uuid4())
    live_parent = await _add(tenant, "live parent")
    kept_child = await _add(tenant, "child of a live parent", parent=live_parent)
    unrelated = await _add(tenant, "no parent")
    await _delete_without_cascade(deleted_parent)

    passes = await _run(tenant)

    states = await _states(orphan, grandchild, never_written, live_parent, kept_child, unrelated)
    gone = {memory_id for memory_id, (_, deleted_at) in states.items() if deleted_at is not None}
    assert gone == {orphan, grandchild, never_written}
    assert {states[memory_id][0] for memory_id in gone} == {"deleted"}
    assert len({states[memory_id][1] for memory_id in gone}) == 1, "one run, one deleted_at"
    assert sum(p.rows for p in passes) == 3
    assert passes[-1].rows == 0, "the run stops on a pass that finds nothing"


async def test_a_dry_run_changes_nothing_and_counts_the_first_level():
    tenant = _tenant()
    parent = await _add(tenant, "deleted parent")
    orphans = [await _add(tenant, f"orphan {i}", parent=parent) for i in range(2)]
    orphans.append(await _add(tenant, "orphan of a missing parent", parent=uuid.uuid4()))
    await _delete_without_cascade(parent)

    [only] = await _run(tenant, dry_run=True)

    assert (only.rows, only.deleted_parents, only.missing_parents) == (3, 1, 1)
    assert all(deleted_at is None for _, deleted_at in (await _states(*orphans)).values())


async def test_a_run_scoped_to_one_tenant_leaves_the_others():
    tenant, other = _tenant(), _tenant()
    in_tenant = await _add(tenant, "orphan", parent=uuid.uuid4())
    in_other = await _add(other, "orphan", parent=uuid.uuid4())

    await _run(tenant)

    states = await _states(in_tenant, in_other)
    assert states[in_tenant][1] is not None
    assert states[in_other][1] is None


async def test_the_walk_reads_migration_058s_index():
    """Planned with the index as the table's only one (``plan_with_only_index``):
    the question is whether it CAN serve the walk, resumed from a cursor, which is
    the shape of every batch after the first."""
    stmt = _backfill()._walk(tenant_id="o12-plan", after=("o12-plan", "a"), batch=100)
    plan = await plan_with_only_index(stmt, "ix_memories_parent_memory_id")
    assert "ix_memories_parent_memory_id" in plan, plan
