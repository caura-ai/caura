"""Agent CRUD and search-profile endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.schemas import AGENT_FIELDS, orm_to_dict
from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/agents", tags=["Agents"])
_svc = PostgresService()


@router.post("")
async def create_or_update_agent(request: Request) -> dict:
    body: dict = await request.json()
    try:
        agent = await _svc.agent_add(body)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return orm_to_dict(agent, AGENT_FIELDS)


@router.post("/create-only")
async def create_agent_without_upsert(request: Request) -> dict:
    """Materialise a missing agent without changing an existing identity.

    The uniqueness constraint decides the race atomically. Agent-key
    provisioning must use this route so stale existence checks cannot turn
    initial trust/fleet hints into updates of an existing agent.
    """
    body: dict = await request.json()
    tenant_id = body.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise HTTPException(status_code=422, detail="tenant_id is required")
    try:
        agent, created = await _svc.agent_create_only(tenant_id, body)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {**orm_to_dict(agent, AGENT_FIELDS), "created": created}


@router.get("/{agent_id}")
async def get_agent(agent_id: str, tenant_id: str) -> dict:
    agent = await _svc.agent_get_by_id(agent_id, tenant_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return orm_to_dict(agent, AGENT_FIELDS)


@router.get("")
async def list_agents(tenant_id: str, fleet_id: str | None = None) -> list[dict]:
    agents = await _svc.agent_list_by_tenant(tenant_id)
    if fleet_id:
        agents = [a for a in agents if getattr(a, "fleet_id", None) == fleet_id]
    return [orm_to_dict(a, AGENT_FIELDS) for a in agents]


@router.delete("/{agent_id}")
async def delete_agent(agent_id: str, tenant_id: str) -> dict:
    await _svc.agent_delete(agent_id, tenant_id)
    return {"ok": True}


@router.patch("/{agent_id}/trust-level")
async def update_trust_level(agent_id: str, request: Request) -> dict:
    body: dict = await request.json()
    await _svc.agent_update_trust_level(
        agent_id=agent_id,
        tenant_id=body["tenant_id"],
        trust_level=body["trust_level"],
        fleet_id=body.get("fleet_id"),
    )
    return {"ok": True}


@router.patch("/{agent_id}/fleet")
async def update_agent_fleet(agent_id: str, request: Request) -> dict:
    body: dict = await request.json()
    await _svc.agent_update_fleet(
        agent_id=agent_id,
        tenant_id=body["tenant_id"],
        fleet_id=body["fleet_id"],
    )
    return {"ok": True}


@router.patch("/{agent_id}/search-profile")
async def update_search_profile(agent_id: str, request: Request) -> dict:
    body: dict = await request.json()
    # agent_id here is the PK (Agent.id), not the string agent_id
    await _svc.agent_update_search_profile(
        agent_id,
        tenant_id=body["tenant_id"],
        search_profile=body["search_profile"],
    )
    return {"ok": True}


@router.get("/{agent_id}/search-profile")
async def get_search_profile(agent_id: str, tenant_id: str) -> dict:
    agent = await _svc.agent_get_by_id(agent_id, tenant_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {"search_profile": getattr(agent, "search_profile", None)}


@router.post("/{agent_id}/search-profile/reset")
async def reset_search_profile(agent_id: str, request: Request) -> dict:
    body: dict = await request.json()
    tenant_id = body["tenant_id"]
    agent = await _svc.agent_get_by_id(agent_id, tenant_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    await _svc.agent_reset_search_profile(agent.id, tenant_id=tenant_id)
    return {"ok": True}


@router.post("/backfill-from-memories")
async def backfill_from_memories(request: Request) -> dict:
    await request.json()  # consume body (contains tenant_id, unused by service)
    count = await _svc.agent_backfill_from_memories()
    return {"count": count}
