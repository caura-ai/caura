"""Usage metering: per-minute model-call and savings totals (migration 066).

core-api and core-worker buffer every model call's provider-reported tokens,
and the savings measures, in memory (``common.usage``) and flush them here
every ~15 s. A flush is bound to one tenant, a required ``tenant_id`` query
parameter, and every row in it is written under that tenant; a writer sends
one flush per tenant (``common.usage.per_tenant``). Each carries an
idempotency key, so a writer that lost the reply re-sends it and it is
counted once.

The read is bound to one tenant, a required ``tenant_id`` query parameter.
The caller (core-api's ``GET /api/v1/usage``, which checks the caller may read
that tenant) supplies it; this service cannot check who is asking, for the
reason ``tenant_usage.py`` gives.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/usage", tags=["Usage"])
_svc = PostgresService()

_Text = Field(min_length=1, max_length=200)
_Optional = Field(default="", max_length=200)
_Count = Field(default=0, ge=0)


class LlmRow(BaseModel):
    minute: datetime
    agent_id: str = _Optional
    fleet_id: str = _Optional
    operation: str = _Text
    kind: Literal["llm", "embedding"]
    provider: str = _Text
    model: str = _Text
    calls: int = _Count
    failures: int = _Count
    tokens_in: int = _Count
    tokens_out: int = _Count
    tokens_reasoning: int = _Count


class ValueRow(BaseModel):
    minute: datetime
    agent_id: str = _Optional
    fleet_id: str = _Optional
    operation: str = _Optional
    metric: str = _Text
    quantity: int = Field(ge=0)


class UsageFlush(BaseModel):
    batch_id: UUID
    llm: list[LlmRow] = Field(default_factory=list, max_length=20_000)
    value: list[ValueRow] = Field(default_factory=list, max_length=20_000)


@router.post("/flush")
async def flush_usage(tenant_id: str, body: UsageFlush) -> dict:
    """Add one tenant's flush. ``{"applied": false}`` means the batch was already counted."""
    if not tenant_id:
        raise HTTPException(status_code=422, detail="'tenant_id' is required")
    applied = await _svc.usage_flush(
        tenant_id,
        body.batch_id,
        [r.model_dump() for r in body.llm],
        [r.model_dump() for r in body.value],
    )
    return {"applied": applied}


_GroupBy = Literal["day", "agent_id", "fleet_id", "operation", "kind", "provider", "model", "metric"]


def _by_day() -> list[_GroupBy]:
    return ["day"]


class UsageQuery(BaseModel):
    start: datetime
    end: datetime
    group_by: list[_GroupBy] = Field(default_factory=_by_day, max_length=8)

    @field_validator("end")
    @classmethod
    def _after_start(cls, end: datetime, info) -> datetime:
        start = info.data.get("start")
        if start is not None and end <= start:
            raise ValueError("'end' must be after 'start'")
        return end


@router.post("/query")
async def query_usage(tenant_id: str, body: UsageQuery) -> dict:
    """One tenant's totals in ``[start, end)``: ``{"model_calls": [...], "savings": [...]}``."""
    if not tenant_id:
        raise HTTPException(status_code=422, detail="'tenant_id' is required")
    return await _svc.usage_query(
        tenant_id, start=body.start, end=body.end, group_by=list(dict.fromkeys(body.group_by))
    )
