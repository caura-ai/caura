"""Task tracking endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request

from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/tasks", tags=["Tasks"])
_svc = PostgresService()


@router.post("/failures")
async def add_task_failure(request: Request) -> dict:
    body: dict = await request.json()
    memory_id = body.get("memory_id")
    if memory_id is not None:
        memory_id = UUID(memory_id)
    await _svc.task_add_failure(
        task_name=body["task_name"],
        memory_id=memory_id,
        tenant_id=body["tenant_id"],
        error_message=body["error_message"],
        error_traceback=body.get("error_traceback", ""),
    )
    return {"ok": True}
