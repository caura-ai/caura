"""Tenant-listing helpers shared by admin endpoints (CAURA-655)."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.events.lifecycle_purge_request import MEMORY_RETENTION_MAX_DAYS
from common.models.memory import Memory


async def list_active_tenant_ids(db: AsyncSession) -> list[str]:
    """Distinct ``tenant_id`` from non-soft-deleted memories.

    Use for archive ops — orgs with no live memories have nothing to
    archive. CAURA-656 purge needs the broader variant below: an org
    that soft-deleted all its memories is exactly who we want to purge.
    """
    result = await db.execute(select(Memory.tenant_id).where(Memory.deleted_at.is_(None)).distinct())
    return sorted([row[0] for row in result.all()])


async def list_tenants_with_purgeable_memories(db: AsyncSession) -> list[str]:
    """Distinct ``tenant_id`` from soft-deleted memories old enough to
    be eligible for hard-deletion under any org's retention window
    (CAURA-656 purge fanout target).

    Orgs whose soft-deleted rows are all newer than the maximum
    retention window (``MEMORY_RETENTION_MAX_DAYS``) are guaranteed
    no-ops on the purge primitive, so excluding them from the fanout
    keeps the discovery scan bounded as ``memories`` grows. Per-org
    retention may be tighter than the max — the storage primitive
    still no-ops for rows inside the org's specific window.

    Uses ``func.now()`` (DB clock) rather than the Python clock to
    match the storage-side primitive's cutoff and avoid client-clock
    drift across the fanout / consume boundary.
    """
    cutoff = func.now() - timedelta(days=MEMORY_RETENTION_MAX_DAYS)
    result = await db.execute(
        select(Memory.tenant_id)
        .where(Memory.deleted_at.is_not(None))
        .where(Memory.deleted_at < cutoff)
        .distinct()
    )
    return sorted([row[0] for row in result.all()])
