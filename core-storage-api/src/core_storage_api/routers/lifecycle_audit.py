"""Lifecycle audit endpoints (CAURA-655).

The write routes create and advance the per-fanout audit row referenced in the
operations architecture. Read routes expose one row for exact probe correlation
and an uncapped aggregate for deployment health checks. Everything lives here,
not in core-api, to preserve the "no DB outside core-storage-api" rule.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.services.postgres_service import UNSCOPED, PostgresService

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


@router.get("/has-recent-success")
async def has_recent_success(org_id: str, action: str, since_hours: int) -> dict:
    """Dedup gate for CAURA-657 pipeline ops. Returns whether the
    given org+action has a successful audit row within
    ``since_hours``; the consumer skips its run when this is True.

    Range-checks ``since_hours`` to keep the SQL interval bounded — an
    operator passing 0 would short-circuit every check (always False),
    and a runaway negative value would scan effectively all rows.
    """
    if since_hours < 1 or since_hours > 168:
        raise HTTPException(
            status_code=422,
            detail="'since_hours' must be in [1, 168] (hours)",
        )
    found = await _svc.lifecycle_audit_has_recent_success(
        org_id=org_id, action=action, since_hours=since_hours
    )
    return {"has_recent_success": found}


@router.post("/summary")
async def summarize_lifecycle_audits(request: Request) -> dict:
    """Aggregate lifecycle rows under an explicit tenant-scope choice."""
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="request body must be a JSON object")
    if "org_id" not in body:
        raise HTTPException(
            status_code=422,
            detail="'org_id' is required; send null for the admin-wide aggregate",
        )
    raw_org_id = body["org_id"]
    if raw_org_id is not None and not isinstance(raw_org_id, str):
        raise HTTPException(status_code=422, detail="'org_id' must be a string or null")
    since_hours = body.get("since_hours", 30)
    if type(since_hours) is not int or since_hours < 1 or since_hours > 168:
        raise HTTPException(
            status_code=422,
            detail="'since_hours' must be in [1, 168] (hours)",
        )
    triggered_by = body.get("triggered_by")
    if triggered_by is not None and not isinstance(triggered_by, str):
        raise HTTPException(status_code=422, detail="'triggered_by' must be a string or null")
    return await _svc.lifecycle_audit_summary(
        org_id=UNSCOPED if raw_org_id is None else raw_org_id,
        since_hours=since_hours,
        triggered_by=triggered_by,
    )


@router.post("/stranded")
async def list_stranded_lifecycle_audits(request: Request) -> dict:
    """Rows the fanout wrote but never published a message for.

    POST rather than GET, and ``org_id`` required in the body rather
    than an optional query parameter, for the same reason ``/summary``
    is shaped that way: a cross-tenant read has to be spelled out by the
    caller. ``null`` means admin-wide and must be written, not omitted.
    """
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="request body must be a JSON object")
    if "org_id" not in body:
        raise HTTPException(
            status_code=422,
            detail="'org_id' is required; send null for the admin-wide sweep",
        )
    raw_org_id = body["org_id"]
    if raw_org_id is not None and not isinstance(raw_org_id, str):
        raise HTTPException(status_code=422, detail="'org_id' must be a string or null")
    triggered_by = body.get("triggered_by")
    if not isinstance(triggered_by, str) or not triggered_by:
        raise HTTPException(status_code=422, detail="'triggered_by' must be a non-empty string")
    older_than_minutes = body.get("older_than_minutes", 30)
    if type(older_than_minutes) is not int or not (1 <= older_than_minutes <= 10080):
        raise HTTPException(
            status_code=422,
            detail="'older_than_minutes' must be in [1, 10080] (minutes)",
        )
    limit = body.get("limit", 200)
    if type(limit) is not int or not (1 <= limit <= 1000):
        raise HTTPException(status_code=422, detail="'limit' must be in [1, 1000]")
    rows = await _svc.lifecycle_audit_list_stranded(
        org_id=UNSCOPED if raw_org_id is None else raw_org_id,
        triggered_by=triggered_by,
        older_than_minutes=older_than_minutes,
        limit=limit,
    )
    return {
        "org_id": raw_org_id,
        "triggered_by": triggered_by,
        "older_than_minutes": older_than_minutes,
        "limit": limit,
        "rows": rows,
    }


@router.get("/{audit_id}")
async def get_lifecycle_audit(audit_id: int, org_id: str) -> dict:
    """Return one audit row so a caller can follow its exact message."""
    row = await _svc.lifecycle_audit_get(audit_id, org_id=org_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"lifecycle_audit {audit_id} not found")
    return row


@router.patch("/{audit_id}")
async def update_lifecycle_audit(audit_id: int, request: Request) -> dict:
    """Update status (+ optional stats / error_message). Body:
    ``{org_id, status, stats?, error_message?}``. ``finished_at`` is stamped
    server-side when ``status`` is terminal (``success``/``failure``).
    """
    try:
        body: dict = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="request body must be a JSON object")
    org_id = body.get("org_id")
    if not isinstance(org_id, str) or not org_id:
        raise HTTPException(status_code=422, detail="'org_id' must be a non-empty string")
    status = body.get("status")
    if status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"status must be one of {sorted(_VALID_STATUSES)}, got {status!r}",
        )
    claim_token = body.get("claim_token")
    if claim_token is not None and not isinstance(claim_token, str):
        raise HTTPException(status_code=422, detail="'claim_token' must be a string when provided")
    result = await _svc.lifecycle_audit_finalize(
        audit_id,
        org_id=org_id,
        status=status,
        stats=body.get("stats"),
        error_message=body.get("error_message"),
        claim_token=claim_token,
    )
    if result == "missing":
        raise HTTPException(status_code=404, detail=f"lifecycle_audit {audit_id} not found")
    # ``updated``, ``noop_success`` (a redelivery of an acked message) and
    # ``claim_conflict`` all return 200. The no-op path used to share the 404
    # branch, which produced spurious "not found" warnings on every redelivery
    # of a successful message. ``claim_conflict`` is a 200 rather than a 409
    # because the consumer wraps this call in a broad ``except`` that logs and
    # CONTINUES — an exception here would be swallowed into "continuing" and
    # the duplicate would run the primitive anyway, which is the whole failure
    # this signal exists to prevent. A field it must read cannot be ignored by
    # an error handler that was written for a different case.
    return {
        "ok": True,
        "noop": result == "noop_success",
        "claim_conflict": result == "claim_conflict",
        # Reported for the same reason ``claim_conflict`` is: the consumer
        # wraps its terminal write in a broad ``except`` that logs and
        # continues, so a raised error here would be swallowed and the
        # duplicate run would leave no trace. This one is not actionable by
        # the caller -- the work has already happened twice -- so it exists to
        # be recorded, not to change control flow.
        "claim_lost": result == "claim_lost",
    }
