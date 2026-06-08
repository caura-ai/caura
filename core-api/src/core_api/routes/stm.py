"""STM (Short-Term Memory) REST endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from core_api.auth import AuthContext, get_auth_context
from core_api.config import settings
from core_api.db.session import get_db

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stm"])


def _check_stm_enabled() -> None:
    if not settings.use_stm:
        raise HTTPException(
            status_code=422,
            detail="STM is not enabled. Set USE_STM=true to enable short-term memory.",
        )


def _require_tenant(auth: AuthContext) -> str:
    if not auth.tenant_id:
        raise HTTPException(status_code=401, detail="Tenant context required")
    return auth.tenant_id


# ---------------------------------------------------------------------------
# Notes (per-agent private)
# ---------------------------------------------------------------------------


@router.get("/stm/notes")
async def get_notes(
    auth: AuthContext = Depends(get_auth_context),
    agent_id: str = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
):
    _check_stm_enabled()
    tenant_id = _require_tenant(auth)
    from core_api.services.stm_service import read_notes

    notes = await read_notes(tenant_id, agent_id, limit=limit)
    return {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "count": len(notes),
        "notes": notes,
    }


@router.delete("/stm/notes")
async def clear_notes(
    auth: AuthContext = Depends(get_auth_context),
    agent_id: str = Query(...),
):
    _check_stm_enabled()
    tenant_id = _require_tenant(auth)
    from core_api.services.stm_service import clear_notes

    await clear_notes(tenant_id, agent_id)
    return {"ok": True, "tenant_id": tenant_id, "agent_id": agent_id}


# ---------------------------------------------------------------------------
# Bulletin (per-fleet shared)
# ---------------------------------------------------------------------------


@router.get("/stm/bulletin")
async def get_bulletin(
    auth: AuthContext = Depends(get_auth_context),
    fleet_id: str = Query(...),
    limit: int = Query(default=100, ge=1, le=500),
):
    _check_stm_enabled()
    tenant_id = _require_tenant(auth)
    from core_api.services.stm_service import read_bulletin

    entries = await read_bulletin(tenant_id, fleet_id, limit=limit)
    return {
        "tenant_id": tenant_id,
        "fleet_id": fleet_id,
        "count": len(entries),
        "bulletin": entries,
    }


@router.delete("/stm/bulletin")
async def clear_bulletin(
    auth: AuthContext = Depends(get_auth_context),
    fleet_id: str = Query(...),
):
    _check_stm_enabled()
    tenant_id = _require_tenant(auth)
    from core_api.services.stm_service import clear_bulletin

    await clear_bulletin(tenant_id, fleet_id)
    return {"ok": True, "tenant_id": tenant_id, "fleet_id": fleet_id}


# ---------------------------------------------------------------------------
# Promote (STM → LTM)
# ---------------------------------------------------------------------------


class PromoteRequest(BaseModel):
    agent_id: str
    content: str
    fleet_id: str | None = None
    memory_type: str | None = None
    visibility: str | None = None


@router.post("/stm/promote")
async def promote_stm(
    body: PromoteRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    _check_stm_enabled()
    tenant_id = _require_tenant(auth)
    from core_api.services.stm_service import promote

    result = await promote(
        content=body.content,
        db=db,
        tenant_id=tenant_id,
        agent_id=body.agent_id,
        fleet_id=body.fleet_id,
        memory_type=body.memory_type,
        visibility=body.visibility,
    )
    return result
