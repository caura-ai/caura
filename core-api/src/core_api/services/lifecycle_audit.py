"""Tiny wrappers around the lifecycle_audit storage routes (CAURA-655).

Two helpers, one for each transition:

* :func:`audit_begin` — creates a ``pending`` row and returns its id.
  Called by the fanout endpoint just before each per-org Pub/Sub
  publish, so the published event carries the row id.
* :func:`make_storage_adapter` — wraps the storage client into the
  :class:`LifecycleStorageAdapter` shape the shared handler expects.
  Used in OSS standalone where core-api itself subscribes to the
  in-process bus (no separate worker process).
"""

from __future__ import annotations

from common.events.lifecycle_handlers import LifecycleStorageAdapter
from core_api.clients.storage_client import CoreStorageClient
from core_api.constants import LIFECYCLE_STALE_ARCHIVE_WEIGHT


async def audit_begin(
    storage: CoreStorageClient,
    *,
    action: str,
    org_id: str,
    triggered_by: str,
) -> int:
    return await storage.create_lifecycle_audit_row(org_id=org_id, action=action, triggered_by=triggered_by)


class _CoreApiLifecycleAdapter:
    """Adapt :class:`CoreStorageClient` to :class:`LifecycleStorageAdapter`.

    The shared handler's protocol takes ``org_id`` (the project's
    canonical key for org-scoped operations); the storage client's
    archive primitives still call the column ``tenant_id``. Translate
    at the boundary so the rename can land here without churning every
    call site of the storage client.
    """

    def __init__(self, storage: CoreStorageClient) -> None:
        self._storage = storage

    async def archive_expired(self, *, org_id: str, fleet_id: str | None) -> int:
        return await self._storage.archive_expired(org_id, fleet_id)

    async def archive_stale(self, *, org_id: str, fleet_id: str | None) -> int:
        return await self._storage.archive_stale(org_id, fleet_id, max_weight=LIFECYCLE_STALE_ARCHIVE_WEIGHT)

    async def update_lifecycle_audit_row(
        self,
        audit_id: int,
        *,
        status: str,
        stats: dict | None = None,
        error_message: str | None = None,
    ) -> None:
        await self._storage.update_lifecycle_audit_row(
            audit_id, status=status, stats=stats, error_message=error_message
        )


def make_storage_adapter(storage: CoreStorageClient) -> LifecycleStorageAdapter:
    return _CoreApiLifecycleAdapter(storage)
