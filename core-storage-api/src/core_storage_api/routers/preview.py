"""Org/tenant deletion-preview endpoint (CAURA-696).

Read-only per-table row counts for a ``tenant_id`` across the OSS
schema. Drives the "what will be deleted?" panel that operators see
before triggering a hard-delete (and that customers see before
self-delete, CAURA-697). The counts mirror exactly what
``purge_tenant_data`` (CAURA-689) would remove — same table set,
same scoping column — so the preview is a faithful forecast.

Trust model (same as ``routers/purge.py``): no router-level
authentication; storage trusts the upstream auth at core-api
(``auth.enforce_admin`` on the preview admin route). The deployment
control is that the storage service is VPC-internal — do NOT expose
this surface to the public internet.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/preview", tags=["Preview"])
_svc = PostgresService()


@router.post("/tenant-counts")
async def preview_tenant_counts(request: Request) -> dict:
    """Per-table row count for a tenant. Body: ``{tenant_id}``.

    Returns ``{tenant_id, counts: {table: row_count}}``. Tables with
    no rows are reported as ``0`` so the caller gets the full
    breakdown without round-tripping for each missing entry.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # ``UnicodeDecodeError`` is NOT a subclass of
        # ``JSONDecodeError``; catching both matches the
        # ``tenant_suppression`` router's hardened shape (PR #244
        # round 1).
        raise HTTPException(status_code=422, detail="request body must be valid JSON") from exc
    # Reject non-object JSON bodies — ``body.get`` on a list / string
    # would raise ``AttributeError`` and surface as 500. Same defence
    # PR #244 round 1 added to ``tenant_suppression``.
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="request body must be a JSON object")
    tenant_id = body.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise HTTPException(
            status_code=422,
            detail="'tenant_id' is required and must be a non-empty string",
        )
    counts = await _svc.count_tenant_data(tenant_id)
    return {"tenant_id": tenant_id, "counts": counts}
