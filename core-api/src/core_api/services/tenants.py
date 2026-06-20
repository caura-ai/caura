"""Tenant-listing helpers shared by admin endpoints (CAURA-655).

These read through core-storage-api (Fix 2 Phase 1) — the discovery queries no
longer touch a core-api DB pool (the underlying SQL now lives in
``postgres_service.tenants_list_*``). The ``db`` parameter is retained for
call-site back-compat and is IGNORED.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from core_api.clients.storage_client import get_storage_client


async def list_active_tenant_ids(db: AsyncSession | None = None) -> list[str]:
    """Distinct ``tenant_id`` from non-soft-deleted memories, sorted.

    Use for archive ops — orgs with no live memories have nothing to archive.
    CAURA-656 purge needs the broader variant below. ``db`` is ignored (reads
    via core-storage-api).
    """
    return await get_storage_client().list_active_tenants()


async def list_tenants_with_purgeable_memories(db: AsyncSession | None = None) -> list[str]:
    """Distinct ``tenant_id`` from soft-deleted memories old enough to be
    eligible for hard-deletion under any org's retention window (CAURA-656 purge
    fanout target), sorted.

    Orgs whose soft-deleted rows are all newer than ``MEMORY_RETENTION_MAX_DAYS``
    are guaranteed purge no-ops, so the storage query excludes them to keep the
    discovery scan bounded as ``memories`` grows (cutoff uses the DB clock).
    ``db`` is ignored (reads via core-storage-api).
    """
    return await get_storage_client().list_purgeable_tenants()


async def list_tenants_with_skills_factory_enabled(db: AsyncSession | None = None) -> list[str]:
    """Return ``org_id`` values whose ``skills_factory.enabled`` is True, sorted.

    Used by the lifecycle-fanout entry point for the ``forge-distill`` action so
    a tenant that hasn't opted in pays ZERO per-cron-tick cost (no message, no
    audit row, no consumer work). Orgs without a settings row are excluded
    (default ``enabled=False``). ``db`` is ignored (reads via core-storage-api).
    """
    return await get_storage_client().list_skills_factory_enabled_orgs()
