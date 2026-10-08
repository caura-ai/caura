"""Task tracking endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from core_storage_api.routers._validation import _require
from core_storage_api.services.postgres_service import (
    TASK_HANDLED_STATUSES,
    TASK_OPEN_STATUSES,
    PostgresService,
)

#: Outcomes a NEW ``background_task_log`` row may be written with. "failed" is
#: the raise path; "cancelled" is work a shutdown stopped (OSS 09/02 M-56).
#: Both are open: ``POST /tasks/failures/handled`` moves a row to one of
#: ``TASK_HANDLED_STATUSES`` once a sweep has dealt with it, which no new row
#: may claim to be.
_TASK_STATUSES = frozenset(TASK_OPEN_STATUSES)

# One mark call; a sweep's batch is far smaller.
_MAX_MARK_IDS = 500

router = APIRouter(prefix="/tasks", tags=["Tasks"])
_svc = PostgresService()


@router.post("/failures")
async def add_task_failure(request: Request) -> dict:
    body: dict = await request.json()
    memory_id = body.get("memory_id")
    if memory_id is not None:
        memory_id = UUID(memory_id)
    # Closed set rather than a passthrough. Not an index concern — extra
    # distinct values in a btree's second column are fine; the point is that a
    # typo'd status creates a bucket nobody knows to query, in a table whose
    # whole value is being queryable by status.
    status = body.get("status", "failed")
    if status not in _TASK_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"'status' must be one of {sorted(_TASK_STATUSES)}",
        )
    await _svc.task_add_failure(
        task_name=body["task_name"],
        memory_id=memory_id,
        tenant_id=body["tenant_id"],
        error_message=body["error_message"],
        error_traceback=body.get("error_traceback", ""),
        status=status,
    )
    return {"ok": True}


@router.get("/failures")
async def list_open_task_failures(
    tenant_id: str,
    task_name: Annotated[list[str], Query(min_length=1)],
    since: datetime,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    memory_id: UUID | None = None,
    max_reruns_per_memory: Annotated[int | None, Query(ge=1)] = None,
) -> list[dict]:
    """A tenant's task rows still ``failed`` or ``cancelled``, oldest first."""
    return await _svc.task_list_open_failures(
        tenant_id=tenant_id,
        task_names=task_name,
        since=since,
        limit=limit,
        memory_id=memory_id,
        max_reruns_per_memory=max_reruns_per_memory,
    )


@router.post("/failures/handled")
async def mark_task_failures_handled(request: Request) -> dict:
    """Move a tenant's open task rows to a handled status; ``updated`` counts them."""
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    status = body.get("status")
    if status not in TASK_HANDLED_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"'status' must be one of {sorted(TASK_HANDLED_STATUSES)}",
        )
    raw_ids = body.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > _MAX_MARK_IDS:
        raise HTTPException(status_code=422, detail=f"'ids' must be a list of 1 to {_MAX_MARK_IDS} row ids")
    try:
        ids = [UUID(str(i)) for i in raw_ids]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="'ids' must hold row ids") from exc
    updated = await _svc.task_mark_handled(tenant_id=tenant_id, ids=ids, status=status)
    return {"updated": updated}
