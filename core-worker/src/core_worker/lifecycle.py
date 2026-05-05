"""Adapter wiring core-worker's httpx storage client into the shared
:class:`common.events.lifecycle_handlers.LifecycleStorageAdapter`
protocol (CAURA-655).

The worker's storage_client is module-level functions, not a class, so
this thin object does the binding once at startup and exposes the
three methods the shared handler needs.
"""

from __future__ import annotations

from common.events.lifecycle_handlers import LifecycleStorageAdapter
from core_worker.clients.storage_client import (
    archive_expired,
    archive_stale,
    get_storage_client,
    purge_soft_deleted,
    update_lifecycle_audit_row,
)


class _CoreWorkerLifecycleAdapter:
    async def archive_expired(self, *, org_id: str, fleet_id: str | None) -> int:
        return await archive_expired(get_storage_client(), tenant_id=org_id, fleet_id=fleet_id)

    async def archive_stale(self, *, org_id: str, fleet_id: str | None) -> int:
        return await archive_stale(get_storage_client(), tenant_id=org_id, fleet_id=fleet_id)

    async def purge_soft_deleted(self, *, org_id: str, fleet_id: str | None, retention_days: int) -> int:
        return await purge_soft_deleted(
            get_storage_client(),
            tenant_id=org_id,
            fleet_id=fleet_id,
            retention_days=retention_days,
        )

    async def update_lifecycle_audit_row(
        self,
        audit_id: int,
        *,
        status: str,
        stats: dict | None = None,
        error_message: str | None = None,
    ) -> None:
        await update_lifecycle_audit_row(
            get_storage_client(),
            audit_id,
            status=status,
            stats=stats,
            error_message=error_message,
        )


def make_storage_adapter() -> LifecycleStorageAdapter:
    return _CoreWorkerLifecycleAdapter()
