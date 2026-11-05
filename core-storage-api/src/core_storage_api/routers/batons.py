"""Handoff batons: which agents a baton is waiting on (migration 076).

Handoff batons, D4 (``docs/plans/handoff-batons.md``): the recall fallback for
a receiver that is not listening on the bus. Every route is bound to one
tenant, a required ``tenant_id`` query parameter, as the correction routes are.
The bus records the receivers when it sends a baton (``POST
/batons/{id}/recipients``); core-api reads the waiting ones on recall
(``/notices``), records an accept (``/accept``, from the receiver or the bus)
and when it last read the bus (``/notices/checked``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from common.models.baton_recipient import GOAL_MAX_CHARS
from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/batons", tags=["Handoff batons"])
_svc = PostgresService()


def _require_tenant(tenant_id: str) -> None:
    if not tenant_id:
        raise HTTPException(status_code=422, detail="'tenant_id' is required")


class RecipientIn(BaseModel):
    agent_id: str = Field(min_length=1, max_length=300)
    message_id: str | None = Field(default=None, max_length=200)


class RecipientsIn(BaseModel):
    from_agent: str = Field(min_length=1, max_length=300)
    goal: str = Field(min_length=1, max_length=GOAL_MAX_CHARS)
    created_at: datetime
    recipients: list[RecipientIn] = Field(min_length=1, max_length=1000)


@router.post("/{baton_id}/recipients")
async def record_recipients(tenant_id: str, baton_id: UUID, body: RecipientsIn) -> dict:
    """The bus sent the baton to these receivers: ``{"recorded": n}`` (new rows only)."""
    _require_tenant(tenant_id)
    n = await _svc.baton_recipients_record(
        tenant_id,
        str(baton_id),
        from_agent=body.from_agent,
        goal=body.goal,
        created_at=body.created_at,
        recipients=[r.model_dump() for r in body.recipients],
    )
    return {"recorded": n}


@router.get("/notices")
async def list_notices(
    tenant_id: str,
    agent_id: str = Query(..., min_length=1, max_length=300),
    limit: int = Query(2, ge=1, le=10),
) -> dict:
    """Batons still waiting on the agent, newest first.

    ``{"notices": [{baton_id, from_agent, goal, created_at, message_id, bus_checked_at}]}``.
    """
    _require_tenant(tenant_id)
    return {"notices": await _svc.baton_notices_for_agent(tenant_id, agent_id, limit=limit)}


class CheckedIn(BaseModel):
    agent_id: str = Field(min_length=1, max_length=300)
    baton_ids: list[UUID] = Field(min_length=1, max_length=10)


@router.post("/notices/checked")
async def notices_checked(tenant_id: str, body: CheckedIn) -> dict:
    """core-api read the bus state of these rows: ``{"checked": n}``."""
    _require_tenant(tenant_id)
    n = await _svc.baton_bus_checked(tenant_id, body.agent_id, [str(b) for b in body.baton_ids])
    return {"checked": n}


class AcceptIn(BaseModel):
    agent_id: str = Field(min_length=1, max_length=300)
    via: Literal["recall", "bus"] = "recall"
    state: Literal["accepted", "replied"] = "accepted"


@router.post("/{baton_id}/accept")
async def accept(tenant_id: str, baton_id: UUID, body: AcceptIn) -> dict:
    """The receiver took the baton up; its row. 404 when it is not a receiver in the tenant."""
    _require_tenant(tenant_id)
    row = await _svc.baton_accept(tenant_id, str(baton_id), body.agent_id, via=body.via, state=body.state)
    if row is None:
        raise HTTPException(status_code=404, detail="Not a receiver of this baton")
    return row
