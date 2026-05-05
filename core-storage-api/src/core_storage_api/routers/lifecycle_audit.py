"""Lifecycle audit endpoints (CAURA-655).

Two routes back the per-fanout audit row referenced in the operations
architecture: ``POST`` creates a ``pending`` row and returns its id;
``PATCH`` flips the row to ``in_progress`` (worker on receipt) or
``success`` / ``failure`` (worker on completion). Lives here, not in
core-api, to preserve the "no DB outside core-storage-api" rule —
both core-api and core-worker call these via their existing
storage_clients.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/lifecycle-audit", tags=["Lifecycle"])
_svc = PostgresService()

_VALID_STATUSES = frozenset({"in_progress", "success", "failure"})


@router.post("")
async def create_lifecycle_audit(request: Request) -> dict:
    """Create a ``pending`` row. Body: ``{org_id, action, triggered_by}``."""
    try:
        body: dict = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="request body must be valid JSON") from exc
    missing = {"org_id", "action", "triggered_by"} - body.keys()
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"missing required fields: {sorted(missing)}",
        )
    audit_id = await _svc.lifecycle_audit_create(
        org_id=body["org_id"],
        action=body["action"],
        triggered_by=body["triggered_by"],
    )
    return {"audit_id": audit_id}


@router.patch("/{audit_id}")
async def update_lifecycle_audit(audit_id: int, request: Request) -> dict:
    """Update status (+ optional stats / error_message). Body:
    ``{status, stats?, error_message?}``. ``finished_at`` is stamped
    server-side when ``status`` is terminal (``success``/``failure``).
    """
    try:
        body: dict = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="request body must be valid JSON") from exc
    status = body.get("status")
    if status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {sorted(_VALID_STATUSES)}, got {status!r}",
        )
    result = await _svc.lifecycle_audit_finalize(
        audit_id,
        status=status,
        stats=body.get("stats"),
        error_message=body.get("error_message"),
    )
    if result is False:
        raise HTTPException(status_code=404, detail=f"lifecycle_audit {audit_id} not found")
    # ``True`` (updated) and ``None`` (no-op against an already-success
    # row — a Pub/Sub redelivery of an acked message) both return 200.
    # The no-op path used to share the 404 branch, which produced
    # spurious "not found" warnings in the consumer's logs on every
    # redelivery of a successful message.
    return {"ok": True, "noop": result is None}
