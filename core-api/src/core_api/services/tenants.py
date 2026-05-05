"""Tenant-listing helpers shared by admin endpoints (CAURA-655)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.memory import Memory


async def list_active_tenant_ids(db: AsyncSession) -> list[str]:
    """Distinct ``tenant_id`` from non-soft-deleted memories.

    OSS-standalone resolves to one id (the standalone tenant). Enterprise
    sees one row per tenant with live memories. Multi-tenant orgs share an
    org_id only at the enterprise side, so OSS-side fanout issues one
    message per tenant — cross-tenant rollups can come later if a shared
    lifecycle policy becomes a thing.
    """
    result = await db.execute(select(Memory.tenant_id).where(Memory.deleted_at.is_(None)).distinct())
    return sorted([row[0] for row in result.all()])
