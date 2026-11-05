"""Memory exposure: which agents a memory was handed to (migration 072).

Correction propagation, D1 (``docs/plans/correction-propagation.md``). Both
routes are bound to one tenant, a required ``tenant_id`` query parameter, as
the value-metering routes are: core-api sends one batch per tenant. Ids and
counts only.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/memory-exposure", tags=["Memory exposure"])
_svc = PostgresService()


def _require_tenant(tenant_id: str) -> None:
    if not tenant_id:
        raise HTTPException(status_code=422, detail="'tenant_id' is required")


class ExposureBatch(BaseModel):
    agent_id: str = Field(min_length=1, max_length=200)
    # A read returns at most ``MAX_SEARCH_TOP_K`` memories; the cap is headroom.
    memory_ids: list[UUID] = Field(min_length=1, max_length=500)
    seen_at: datetime
    trace_id: str | None = Field(default=None, max_length=200)


@router.post("")
async def record_exposure(tenant_id: str, body: ExposureBatch) -> dict:
    """One read by one agent of these memories: ``{"recorded": n}``.

    Ids that name no memory of the tenant are skipped, not refused.
    """
    _require_tenant(tenant_id)
    recorded = await _svc.exposure_record(
        tenant_id,
        agent_id=body.agent_id,
        memory_ids=[str(m) for m in body.memory_ids],
        seen_at=body.seen_at,
        trace_id=body.trace_id,
    )
    return {"recorded": recorded}


@router.get("/{memory_id}")
async def get_exposure(tenant_id: str, memory_id: UUID) -> dict:
    """The agents ``memory_id`` was handed to, latest read first: ``{"agents": [...]}``."""
    _require_tenant(tenant_id)
    return {"agents": await _svc.exposure_for_memory(tenant_id, str(memory_id))}
