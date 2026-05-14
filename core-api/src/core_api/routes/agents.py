from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.db.session import get_db
from core_api.schemas import AgentOut, AgentTrustUpdate, SearchProfileUpdate
from core_api.services.agent_service import update_trust_level
from core_api.services.audit_service import log_action
from core_api.services.organization_settings import validate_search_profile

router = APIRouter(tags=["Admin"])


@router.get("/agents", response_model=list[AgentOut])
async def list_agents(
    tenant_id: str = Query(...),
    fleet_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
):
    """List all registered agents for a tenant with their trust levels."""
    auth.enforce_tenant(tenant_id)
    sc = get_storage_client()
    agents = await sc.list_agents(tenant_id, fleet_id=fleet_id)
    return [AgentOut.model_validate(a) for a in agents]


@router.get("/agents/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: str,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
):
    """Get a single agent's details and trust level."""
    auth.enforce_tenant(tenant_id)
    sc = get_storage_client()
    agent = await sc.get_agent(agent_id, tenant_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return AgentOut.model_validate(agent)


@router.patch("/agents/{agent_id}/trust", response_model=AgentOut)
async def patch_agent_trust(
    agent_id: str,
    body: AgentTrustUpdate,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Update an agent's trust level (and optionally fleet)."""
    auth.enforce_tenant(tenant_id)
    agent = await update_trust_level(
        db,
        tenant_id,
        agent_id,
        body.trust_level,
        fleet_id=body.fleet_id,
    )
    await db.commit()
    return AgentOut.model_validate(agent)


@router.patch("/agents/{agent_id}/fleet")
async def update_agent_fleet(
    agent_id: str,
    body: dict,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
):
    """Reassign an agent's home fleet."""
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    auth.enforce_tenant(tenant_id)
    fleet_id = body.get("fleet_id")
    if not fleet_id:
        raise HTTPException(status_code=400, detail="fleet_id is required")

    sc = get_storage_client()
    agent = await sc.get_agent(agent_id, tenant_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

    old_fleet = agent.get("fleet_id")
    await sc.update_agent_fleet(agent_id, {"tenant_id": tenant_id, "fleet_id": fleet_id})
    return {"agent_id": agent_id, "old_fleet_id": old_fleet, "new_fleet_id": fleet_id}


@router.get("/agents/{agent_id}/tune", response_model=AgentOut)
async def get_agent_tune(
    agent_id: str,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
):
    """Get an agent's current search profile (retrieval tuning parameters)."""
    auth.enforce_tenant(tenant_id)
    sc = get_storage_client()
    agent = await sc.get_agent(agent_id, tenant_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return AgentOut.model_validate(agent)


@router.patch("/agents/{agent_id}/tune", response_model=AgentOut)
async def patch_agent_tune(
    agent_id: str,
    body: SearchProfileUpdate,
    tenant_id: str = Query(...),
    reset: bool = Query(default=False),
    auth: AuthContext = Depends(get_auth_context),
):
    """Update an agent's search profile (per-agent retrieval tuning). Pass ?reset=true to clear."""
    auth.enforce_tenant(tenant_id)
    sc = get_storage_client()
    agent = await sc.get_agent(agent_id, tenant_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

    if reset:
        updated = await sc.reset_search_profile(agent_id, tenant_id)
        if not updated:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        return AgentOut.model_validate(updated)

    # Merge: only set non-None fields, preserve existing profile values
    current = agent.get("search_profile") or {}
    updates = body.model_dump(exclude_none=True)
    if updates:
        current.update(updates)
        current = validate_search_profile(current)
        await sc.update_search_profile(agent["id"], {"tenant_id": tenant_id, "search_profile": current})
        # Re-fetch to get the updated agent with full fields
        refreshed = await sc.get_agent(agent_id, tenant_id)
        if refreshed:
            return AgentOut.model_validate(refreshed)
    return AgentOut.model_validate(agent)


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Delete an agent. Memories written by this agent are NOT deleted."""
    auth.enforce_read_only()
    auth.enforce_tenant(tenant_id)
    sc = get_storage_client()
    agent = await sc.get_agent(agent_id, tenant_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    await log_action(
        db,
        tenant_id=tenant_id,
        action="delete",
        resource_type="agent",
        detail={"agent_id": agent_id, "fleet_id": agent.get("fleet_id")},
    )
    await sc.delete_agent(agent_id, tenant_id)
    await db.commit()
