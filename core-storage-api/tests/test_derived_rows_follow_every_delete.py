"""B25 (M-52, M-53) on the migrated schema: derived rows follow every delete.

Auto-chunk and atomic-fact children link to their parent only through
``metadata.parent_memory_id``. Each storage delete primitive now soft-deletes the
live rows derived from what it deletes, in the same transaction, and migration
058's partial index serves that lookup. Run against the real migration chain,
where ``memories.metadata`` is ``json``, because that is what the predicate and
the index expression have to agree on in production.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql

from common.models import Memory
from core_storage_api.database.init import get_engine
from core_storage_api.services import postgres_service
from core_storage_api.services.postgres_service import PostgresService

pytestmark = pytest.mark.asyncio


async def _add(svc: PostgresService, tenant: str, content: str, *, parent=None, metadata=None, **fields):
    metadata = dict(metadata or {})
    if parent is not None:
        metadata["parent_memory_id"] = str(parent)
        metadata.setdefault("source", "auto_chunk")
    row = await svc.memory_add(
        {
            "tenant_id": tenant,
            "agent_id": "b25-agent",
            "content": f"{content} {uuid.uuid4()}",
            "memory_type": "fact",
            "status": "active",
            "visibility": "scope_team",
            "metadata_": metadata,
            **fields,
        }
    )
    return row.id


async def _live(*ids) -> set:
    async with get_engine().connect() as conn:
        rows = await conn.execute(
            text("SELECT id FROM memories WHERE id = ANY(:ids) AND deleted_at IS NULL"), {"ids": list(ids)}
        )
        return {r[0] for r in rows}


def _tenant() -> str:
    return f"b25-{uuid.uuid4().hex[:8]}"


async def test_a_delete_by_id_takes_the_derived_rows_and_stays_in_its_tenant(_ensure_schema):
    svc = PostgresService()
    tenant = _tenant()
    parent = await _add(svc, tenant, "parent")
    child = await _add(svc, tenant, "child", parent=parent)
    # Another tenant's row naming the same parent id is not this tenant's to delete.
    foreign = await _add(svc, _tenant(), "foreign", parent=parent)

    assert await svc.memory_soft_delete_by_ids(tenant, [parent]) == 2
    assert await _live(parent, child, foreign) == {foreign}


async def test_a_filter_delete_takes_the_derived_rows_but_keeps_an_excluded_one(_ensure_schema):
    svc = PostgresService()
    tenant = _tenant()
    parent = await _add(svc, tenant, "parent", metadata={"cleanup_tag": "b25"})
    child = await _add(svc, tenant, "child", parent=parent)
    kept = await _add(svc, tenant, "kept child", parent=parent)

    deleted = await svc.memory_soft_delete_by_filter(
        tenant_id=tenant, metadata_filter={"cleanup_tag": "b25"}, exclude_ids=[kept]
    )

    assert deleted == 2
    assert await _live(parent, child, kept) == {kept}


async def test_an_undo_takes_the_fan_out_children_of_the_run(_ensure_schema):
    """M-52: the children carry ``source = "atomic_fact_fanout"`` and may have no run."""
    svc = PostgresService()
    tenant = _tenant()
    fact = await _add(svc, tenant, "ingested", metadata={"source": "ingest"}, run_id="b25-run")
    child = await _add(svc, tenant, "fan-out", parent=fact, metadata={"source": "atomic_fact_fanout"})

    assert await svc.memory_soft_delete_by_run(tenant, "b25-run") == 2
    assert await _live(fact, child) == set()


async def test_the_single_row_delete_leaves_derived_rows_to_its_caller(client):
    """``DELETE /memories/{id}`` deletes the one row. Both of its callers delete and
    audit each child themselves, and governance remediation must audit a child
    before deleting it; a cascade here would delete the children first."""
    svc = PostgresService()
    tenant = _tenant()
    parent = await _add(svc, tenant, "parent")
    child = await _add(svc, tenant, "child", parent=parent)

    resp = await client.delete(f"/api/v1/storage/memories/{parent}", params={"tenant_id": tenant})

    assert resp.status_code == 200, resp.text
    assert await _live(parent, child) == {child}


async def test_the_derived_row_lookup_can_use_the_index(_ensure_schema):
    """The cost claim, checked against the planner rather than asserted.

    ``enable_seqscan`` is off because on a near-empty table a sequential scan is
    legitimately cheaper; the question is whether migration 058's index CAN serve
    the predicate every cascade and ``memory_find_children_by_parent_id`` use.
    """
    stmt = select(Memory.id).where(
        *postgres_service.derived_rows_where("b25-plan", ["00000000-0000-0000-0000-000000000001"])
    )
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    async with get_engine().connect() as conn:
        await conn.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(r[0] for r in (await conn.execute(text(f"EXPLAIN {sql}"))).all())
    assert "ix_memories_parent_memory_id" in plan, plan
