"""Tenant-listing helpers shared by admin endpoints (CAURA-655)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.memory import Memory


async def list_active_tenant_ids(db: AsyncSession) -> list[str]:
    """Distinct ``tenant_id`` from non-soft-deleted memories.

    Use for archive ops — orgs with no live memories have nothing to
    archive. CAURA-656 purge needs the broader variant below: an org
    that soft-deleted all its memories is exactly who we want to purge.
    """
    result = await db.execute(select(Memory.tenant_id).where(Memory.deleted_at.is_(None)).distinct())
    return sorted([row[0] for row in result.all()])


async def list_tenants_with_any_memory(db: AsyncSession) -> list[str]:
    """Distinct ``tenant_id`` from EVERY memory row, including
    soft-deleted ones (CAURA-656 purge fanout target).

    The narrower :func:`list_active_tenant_ids` filters
    ``deleted_at IS NULL`` and would silently skip an org whose
    memories are 100% soft-deleted — exactly the population the purge
    op needs to run against, so the daily cron would never reclaim
    their storage.
    """
    result = await db.execute(select(Memory.tenant_id).distinct())
    return sorted([row[0] for row in result.all()])
