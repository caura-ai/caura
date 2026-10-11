"""Value metering: agent sessions, memory origins, reuse, and the value read (migration 067).

Every route is bound to one tenant, a required ``tenant_id`` query parameter:
core-api splits its buffered session touches per tenant rather than sending
one cross-tenant batch, so nothing here needs to span tenants. Counts and ids
only. docs/plans/value-metering.md says what each figure means.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/value", tags=["Value"])
_svc = PostgresService()

_Id = Field(min_length=1, max_length=200)
_Optional = Field(default="", max_length=200)


def _require_tenant(tenant_id: str) -> None:
    if not tenant_id:
        raise HTTPException(status_code=422, detail="'tenant_id' is required")


class SessionTouch(BaseModel):
    session_id: str = _Id
    agent_id: str = _Optional
    source: str = Field(default="", max_length=20)
    first_seen: datetime
    last_seen: datetime
    calls: int = Field(default=0, ge=0)


class SessionTouches(BaseModel):
    sessions: list[SessionTouch] = Field(max_length=5000)


@router.post("/sessions")
async def touch_sessions(tenant_id: str, body: SessionTouches) -> dict:
    """Agent sessions seen: earliest first sighting, latest last one, calls added."""
    _require_tenant(tenant_id)
    return {"updated": await _svc.value_touch_sessions(tenant_id, [s.model_dump() for s in body.sessions])}


class Origin(BaseModel):
    memory_id: UUID
    session_id: str = _Id
    session_source: str = Field(default="", max_length=20)
    agent_id: str = _Optional
    written_at: datetime
    origin_calls: int | None = Field(default=None, ge=0)
    origin_tokens_in: int | None = Field(default=None, ge=0)
    origin_tokens_out: int | None = Field(default=None, ge=0)
    origin_turns: int | None = Field(default=None, ge=0)
    origin_source: str | None = Field(default=None, pattern=r"^(hook|plugin|sdk)$")


@router.post("/origins")
async def record_origin(tenant_id: str, body: Origin) -> dict:
    """The work behind one memory, recorded once: ``{"recorded": bool, "origin_seconds": int | None}``."""
    _require_tenant(tenant_id)
    stored = await _svc.value_record_origin(tenant_id, body.model_dump())
    return {"recorded": stored is not None, "origin_seconds": (stored or {}).get("origin_seconds")}


class Reuse(BaseModel):
    session_id: str = _Id
    reader_agent_id: str = _Optional
    memory_ids: list[UUID] = Field(min_length=1, max_length=200)
    recalled_at: datetime


@router.post("/reuse")
async def record_reuse(tenant_id: str, body: Reuse) -> dict:
    """First recalls by this session of memories from other sessions: ``{"recorded": n}``."""
    _require_tenant(tenant_id)
    recorded = await _svc.value_record_reuse(
        tenant_id,
        session_id=body.session_id,
        reader_agent_id=body.reader_agent_id,
        memory_ids=[str(m) for m in body.memory_ids],
        recalled_at=body.recalled_at,
    )
    return {"recorded": recorded}


class ValueQuery(BaseModel):
    start: datetime
    end: datetime
    top: int = Field(default=8, ge=1, le=50)


@router.post("/query")
async def query_value(tenant_id: str, body: ValueQuery) -> dict:
    """One tenant's value figures in ``[start, end)``."""
    _require_tenant(tenant_id)
    if body.end <= body.start:
        raise HTTPException(status_code=422, detail="'end' must be after 'start'")
    return await _svc.value_query(tenant_id, start=body.start, end=body.end, top=body.top)
