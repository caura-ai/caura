"""Memory corrections: who to tell about a correction (migration 073).

Correction propagation, CP-10 (``docs/plans/correction-propagation.md``).
Every route is bound to one tenant, a required ``tenant_id`` query parameter,
as the exposure routes are. The consumer of ``caura.memory.corrected`` posts
the event here; core-api reads the preview back.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from common.events.memory_corrected import SUMMARY_MAX_CHARS
from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/memory-corrections", tags=["Memory corrections"])
_svc = PostgresService()


def _require_tenant(tenant_id: str) -> None:
    if not tenant_id:
        raise HTTPException(status_code=422, detail="'tenant_id' is required")


class CorrectionIn(BaseModel):
    event_id: UUID
    memory_id: UUID
    kind: Literal["content_edit", "status", "unverify", "conflict_resolve"]
    actor_kind: str = Field(min_length=1, max_length=50)
    actor_id: str | None = Field(default=None, max_length=200)
    corrected_at: datetime
    old_summary: str | None = Field(default=None, max_length=SUMMARY_MAX_CHARS)
    new_summary: str | None = Field(default=None, max_length=SUMMARY_MAX_CHARS)
    detail: dict = Field(default_factory=dict)


@router.post("")
async def record_correction(tenant_id: str, body: CorrectionIn) -> dict:
    """Record a correction and its recipients: ``{id, created, recipients, truncated}``.

    A redelivered event returns the first record with ``created`` false. A
    memory that is no longer in the tenant is ``{"id": null}``: nothing to
    correct, and nothing a retry would change.
    """
    _require_tenant(tenant_id)
    result = await _svc.correction_record(
        tenant_id,
        event_id=str(body.event_id),
        memory_id=str(body.memory_id),
        kind=body.kind,
        actor_kind=body.actor_kind,
        actor_id=body.actor_id,
        corrected_at=body.corrected_at,
        old_summary=body.old_summary,
        new_summary=body.new_summary,
        detail=body.detail,
    )
    return result if result is not None else {"id": None, "created": False}


@router.get("/by-memory/{memory_id}")
async def list_corrections(tenant_id: str, memory_id: UUID, limit: int = Query(20, ge=1, le=100)) -> dict:
    """The memory's corrections, newest first: ``{"corrections": [...]}``."""
    _require_tenant(tenant_id)
    return {"corrections": await _svc.corrections_for_memory(tenant_id, str(memory_id), limit=limit)}


@router.get("/{correction_id}")
async def get_correction(tenant_id: str, correction_id: UUID) -> dict:
    """One correction with its recipients; 404 when it is not the tenant's."""
    _require_tenant(tenant_id)
    found = await _svc.correction_get(tenant_id, str(correction_id))
    if found is None:
        raise HTTPException(status_code=404, detail="Correction not found")
    return found
