"""The retention hard-delete takes the graph mined from the purged memories.

``memory_purge_soft_deleted`` was a plain ``DELETE FROM memories``. Links
cascade, but ``relations.evidence_memory_id`` is ``ON DELETE SET NULL`` — the
triple stayed with no evidence, and a relation with no evidence reads as
"nothing memory-derived" to every agent, so a triple mined from a deleted
private memory became visible to peers precisely because it was deleted. The
entities mined only from it stayed listed too. The purge now runs the
``memory_purge_entity_artifacts`` sequence over the batch first.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from common.models import Entity, Memory, MemoryEntityLink, Relation
from common.models.entity import LINK_SOURCE_EXTRACTION
from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _memory(svc: PostgresService, tenant: str, **extra):
    return await svc.memory_add(
        {
            "tenant_id": tenant,
            "agent_id": "purge-tester",
            "content": f"purge canary {uuid.uuid4()}",
            "memory_type": "fact",
            "weight": 0.5,
            "status": "active",
            "visibility": "scope_agent",
            **extra,
        }
    )


async def _entity(svc: PostgresService, tenant: str, name: str):
    return await svc.entity_add({"tenant_id": tenant, "entity_type": "person", "canonical_name": name})


async def _expire(svc: PostgresService, tenant: str, memory_id) -> None:
    """Soft-delete, then age ``deleted_at`` past any retention window."""
    await svc.memory_soft_delete_by_ids(tenant, [memory_id])
    async with get_session() as s:
        await s.execute(
            update(Memory)
            .where(Memory.id == memory_id)
            .values(deleted_at=datetime.now(UTC) - timedelta(days=400))
        )


async def test_purge_removes_relations_and_orphaned_entities(_ensure_schema):
    svc = PostgresService()
    tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"
    gone = await _memory(svc, tenant)
    kept = await _memory(svc, tenant)
    anna = await _entity(svc, tenant, f"Anna {uuid.uuid4().hex[:6]}")
    zenith = await _entity(svc, tenant, f"Zenith {uuid.uuid4().hex[:6]}")
    shared = await _entity(svc, tenant, f"Shared {uuid.uuid4().hex[:6]}")
    subject_only = await _entity(svc, tenant, f"Subject {uuid.uuid4().hex[:6]}")
    async with get_session() as s:
        for mid, eid in (
            (gone.id, anna.id),
            (gone.id, zenith.id),
            (gone.id, shared.id),
            (kept.id, shared.id),
        ):
            s.add(
                MemoryEntityLink(memory_id=mid, entity_id=eid, role="mentions", source=LINK_SOURCE_EXTRACTION)
            )
        s.add(
            Relation(
                tenant_id=tenant,
                from_entity_id=anna.id,
                relation_type="negotiates",
                to_entity_id=zenith.id,
                evidence_memory_id=gone.id,
            )
        )
        await s.execute(update(Memory).where(Memory.id == gone.id).values(subject_entity_id=subject_only.id))
    await _expire(svc, tenant, gone.id)

    assert await svc.memory_purge_soft_deleted(tenant, retention_days=30) == 1

    async with get_session() as s:
        relations = (await s.execute(select(Relation).where(Relation.tenant_id == tenant))).scalars().all()
        names = set((await s.execute(select(Entity.id).where(Entity.tenant_id == tenant))).scalars().all())
        memories = set((await s.execute(select(Memory.id).where(Memory.tenant_id == tenant))).scalars().all())
    assert not relations, "the evidence-less triple must not outlive its memory"
    assert names == {shared.id}, "only the entity another live memory still links survives"
    assert memories == {kept.id}


async def test_purge_leaves_rows_inside_the_retention_window(_ensure_schema):
    svc = PostgresService()
    tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"
    recent = await _memory(svc, tenant)
    anna = await _entity(svc, tenant, f"Anna {uuid.uuid4().hex[:6]}")
    async with get_session() as s:
        s.add(
            MemoryEntityLink(
                memory_id=recent.id, entity_id=anna.id, role="mentions", source=LINK_SOURCE_EXTRACTION
            )
        )
    await svc.memory_soft_delete_by_ids(tenant, [recent.id])

    assert await svc.memory_purge_soft_deleted(tenant, retention_days=30) == 0
    async with get_session() as s:
        assert (await s.execute(select(Entity.id).where(Entity.id == anna.id))).scalar() == anna.id
