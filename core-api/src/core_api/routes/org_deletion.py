"""Admin org-deletion endpoints (CAURA-689 + CAURA-696).

``POST /admin/org/purge-data`` — permanently purge all OSS (``public.*``)
data for a set of ``tenant_ids`` (every tenant of an enterprise org being
hard-deleted). The enterprise ``enterprise.*`` rows are removed separately
by platform-storage-api; the enterprise platform-admin-api orchestrates
both during the hard-delete sweep / force-delete and calls this route with
the configured ``CORE_API_ADMIN_API_KEY``.

One ``lifecycle_audit`` row is written per tenant_id (action
``hard-delete-org``): a ``pending`` row before the purge, flipped to
``success`` (with per-table counts) or ``failure``. ``lifecycle_audit``
itself is never purged, so this trail survives the deletion it records.

``POST /admin/org/preview-data`` — read-only per-tenant row counts that
mirror exactly what a subsequent purge would delete. Powers the
"what will be deleted?" panel the OPS UI (and the customer self-delete
flow, CAURA-697) shows before the destructive action. No audit row;
this is a forecast, not an event.

Auth: admin-key only (``auth.enforce_admin``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Admin", "Lifecycle"])

_ACTION = "hard-delete-org"
# Cap per call so a single request can't tie up a worker indefinitely — the
# route is opted out of the global request timeout. The orchestrator splits
# larger orgs across multiple calls.
_MAX_TENANT_IDS = 100


@router.post("/admin/org/purge-data")
async def purge_org_data(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    """Purge OSS data for each ``tenant_id``. Body: ``{tenant_ids, triggered_by?}``.

    Returns ``{"purged": {tenant_id: {table: count}}, "failed": {tenant_id: error}}``.
    Per-tenant isolation: one tenant's failure neither aborts the others
    nor rolls back tenants already purged (each purge is its own
    transaction and is idempotent on retry). The caller inspects
    ``failed`` before tearing down the enterprise rows.

    Status code contract — uniform signalling: **200** when every tenant
    purged successfully (``failed`` is empty); **207 Multi-Status** when
    any tenant failed (partial or total). The orchestrator can detect
    failure from the status code alone, without parsing the body.
    """
    auth.enforce_admin()
    try:
        body: dict = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="request body must be valid JSON") from exc

    tenant_ids = body.get("tenant_ids")
    if (
        not isinstance(tenant_ids, list)
        or not tenant_ids
        or not all(isinstance(t, str) and t for t in tenant_ids)
    ):
        raise HTTPException(
            status_code=422,
            detail="'tenant_ids' must be a non-empty list of non-empty strings",
        )
    if len(tenant_ids) > _MAX_TENANT_IDS:
        raise HTTPException(
            status_code=422,
            detail=f"'tenant_ids' must contain at most {_MAX_TENANT_IDS} entries per call",
        )
    if len(tenant_ids) != len(set(tenant_ids)):
        raise HTTPException(status_code=422, detail="'tenant_ids' must not contain duplicates")
    triggered_by = body.get("triggered_by") or "admin-key"

    storage = get_storage_client()
    purged: dict[str, dict[str, int]] = {}
    failed: dict[str, str] = {}

    async def _finalize(audit_id: int | None, **kwargs: Any) -> None:
        # The purge is the source of truth: a transient audit-write failure
        # must never mask a completed purge or abort the rest of the batch.
        if audit_id is None:
            return
        try:
            await storage.update_lifecycle_audit_row(audit_id, **kwargs)
        except Exception:
            logger.warning("failed to update lifecycle_audit row %s", audit_id, exc_info=True)

    for tenant_id in tenant_ids:
        try:
            audit_id: int | None = await storage.create_lifecycle_audit_row(
                org_id=tenant_id, action=_ACTION, triggered_by=triggered_by
            )
        except Exception:
            logger.exception("failed to create lifecycle_audit row for %s", tenant_id)
            audit_id = None

        try:
            counts = await storage.purge_tenant_data(tenant_id)
        except Exception as exc:
            logger.exception("purge_tenant_data failed for tenant %s", tenant_id)
            failed[tenant_id] = str(exc)
            await _finalize(audit_id, status="failure", error_message=str(exc))
            continue

        purged[tenant_id] = counts
        await _finalize(audit_id, status="success", stats={"deleted": counts})

    logger.info(
        "purge_org_data: %d purged, %d failed (triggered_by=%s)",
        len(purged),
        len(failed),
        triggered_by,
    )
    status_code = 207 if failed else 200
    return JSONResponse(status_code=status_code, content={"purged": purged, "failed": failed})


@router.post("/admin/org/preview-data")
async def preview_org_data(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> JSONResponse:
    """Per-tenant row-count forecast for a set of ``tenant_ids`` (CAURA-696).

    Body: ``{tenant_ids}``. Returns
    ``{"counts": {tenant_id: {table: row_count}}, "failed": {tenant_id: error}}``.

    Status code contract mirrors ``purge_org_data``: **200** on a fully
    clean preview (``failed`` empty), **207 Multi-Status** when any
    tenant's count round-trip failed — so the orchestrator's display
    layer can decide whether to render partial data with an "incomplete"
    badge or surface the failure outright.

    Read-only. Per-tenant isolation: one storage hiccup on tenant A
    does not abort the read for tenant B. No ``lifecycle_audit`` row
    written — a preview is a query, not a lifecycle event.
    """
    auth.enforce_admin()
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # Match the hardened tenant-suppression / preview routers — a
        # malformed UTF-8 body would otherwise surface as 500.
        raise HTTPException(status_code=422, detail="request body must be valid JSON") from exc
    # Reject non-object JSON bodies — ``request.json()`` succeeds for
    # arrays / strings / numbers, and ``.get`` on those raises
    # ``AttributeError`` → 500. The storage-layer preview router has
    # the same defence; mirror it here so the public surface stays
    # consistent. Bot review round 1 on PR #246.
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="request body must be a JSON object")

    tenant_ids = body.get("tenant_ids")
    if (
        not isinstance(tenant_ids, list)
        or not tenant_ids
        or not all(isinstance(t, str) and t for t in tenant_ids)
    ):
        raise HTTPException(
            status_code=422,
            detail="'tenant_ids' must be a non-empty list of non-empty strings",
        )
    if len(tenant_ids) > _MAX_TENANT_IDS:
        raise HTTPException(
            status_code=422,
            detail=f"'tenant_ids' must contain at most {_MAX_TENANT_IDS} entries per call",
        )
    if len(tenant_ids) != len(set(tenant_ids)):
        raise HTTPException(status_code=422, detail="'tenant_ids' must not contain duplicates")

    storage = get_storage_client()
    counts: dict[str, dict[str, int]] = {}
    failed: dict[str, str] = {}

    for tenant_id in tenant_ids:
        try:
            counts[tenant_id] = await storage.count_tenant_data(tenant_id)
        except Exception as exc:
            logger.exception("count_tenant_data failed for tenant %s", tenant_id)
            failed[tenant_id] = str(exc)

    status_code = 207 if failed else 200
    return JSONResponse(status_code=status_code, content={"counts": counts, "failed": failed})
