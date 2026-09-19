"""Task tracking endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.services.postgres_service import PostgresService

#: Outcomes ``background_task_log.status`` may hold. "failed" is the raise
#: path; "cancelled" is work a shutdown stopped (OSS 09/02 M-56).
#:
#: Expected to grow, and the row is TERMINAL today: ``task_add_failure`` is
#: insert-only and there is no update path, so a "cancelled" row never leaves
#: that status. Anything that later sweeps and repairs these needs a third
#: value plus a transition, or it will re-select the same rows forever.
_TASK_STATUSES = frozenset({"failed", "cancelled"})

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
