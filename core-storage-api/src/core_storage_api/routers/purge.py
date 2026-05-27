"""Org/tenant hard-purge endpoint (CAURA-689).

Permanently deletes all OSS (``public.*``) data for a ``tenant_id``.
Called by core-api's admin org-purge route during an organization
hard-delete; core-api writes the ``lifecycle_audit`` trail around it.
Kept here, not in core-api, to honor the "no DB outside
core-storage-api" rule.

Trust model (matches the rest of core-storage-api — see
``routers/keystones.py`` for the same statement): no router-level
authentication. Storage trusts that every caller has already passed
the upstream auth check at core-api (``auth.enforce_admin`` on
``POST /admin/org/purge-data``, gated by ``CORE_API_ADMIN_API_KEY``).
The deployment-level control is that the storage service is reachable
only from inside the VPC; do NOT expose it to the public internet.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/purge", tags=["Purge"])
_svc = PostgresService()


@router.post("/tenant-data")
async def purge_tenant_data(request: Request) -> dict:
    """Hard-delete every row scoped to ``tenant_id``. Body: ``{tenant_id}``.

    Returns ``{tenant_id, deleted: {table: count}}``. Idempotent — a
    second call for the same tenant deletes nothing.
    """
    try:
        body: dict = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="request body must be valid JSON") from exc
    tenant_id = body.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise HTTPException(
            status_code=422,
            detail="'tenant_id' is required and must be a non-empty string",
        )
    counts = await _svc.purge_tenant_data(tenant_id)
    return {"tenant_id": tenant_id, "deleted": counts}
