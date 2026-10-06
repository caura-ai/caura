from fastapi import APIRouter, Depends, HTTPException, Query

from core_api import openapi_responses as _oar
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.schemas import AgentOut, AgentTrustUpdate, SearchProfileUpdate
from core_api.services.agent_service import enforce_broker_agent_ownership, lookup_agent, update_trust_level
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
    agent = await lookup_agent(tenant_id, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    return AgentOut.model_validate(agent)


@router.patch("/agents/{agent_id}/trust", response_model=AgentOut)
async def patch_agent_trust(
    agent_id: str,
    body: AgentTrustUpdate,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
):
    """Update an agent's trust level (and optionally fleet)."""
    # A read-only credential must not move the trust ladder. This gate was
    # missing while the neighbouring fleet-reassignment route had it, so a
    # capabilities={'read'} key could rewrite the very control the comment
    # below calls the master key.
    #
    # NOT gated on ``enforce_usage_limits``, unlike that neighbour, and
    # deliberately: this is the route you reach for to DEMOTE a misbehaving
    # agent. An over-quota tenant must still be able to take trust away, so
    # quota state must not stand between an operator and a mitigation.
    auth.enforce_read_only()
    auth.enforce_tenant(tenant_id)
    # Trust changes are the master key to the whole ladder — an agent must not
    # be able to PATCH its own (or a peer's) trust_level to self-promote.
    auth.enforce_not_agent_credential("change agent trust levels")
    auth.enforce_not_org_member("change agent trust levels")
    # The prior values, for the audit row below. Primary: a lagged replica
    # would record the wrong "before" for the change being audited.
    before = await lookup_agent(tenant_id, agent_id, read=False)
    agent = await update_trust_level(
        tenant_id,
        agent_id,
        body.trust_level,
        fleet_id=body.fleet_id,
    )
    # Every trust move leaves a row in the tenant audit log — this is the
    # control the whole ladder hangs off, so who changed it, and from what,
    # has to be answerable after the fact.
    await log_action(
        tenant_id=tenant_id,
        action="agent_trust_update",
        resource_type="agent",
        resource_id=agent.get("id"),
        detail={
            "agent_id": agent.get("agent_id", agent_id),
            "old_trust_level": (before or {}).get("trust_level"),
            "new_trust_level": agent.get("trust_level", body.trust_level),
            "old_fleet_id": (before or {}).get("fleet_id"),
            "new_fleet_id": agent.get("fleet_id"),
            **auth.audit_actor(),
        },
    )
    return AgentOut.model_validate(agent)


@router.patch(
    "/agents/{agent_id}/fleet",
    responses={200: {"model": _oar.AgentFleetPatchResponse}},
)
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
    # Fleet reassignment grants home-fleet access to the target fleet — an agent
    # must not be able to relocate itself/a peer to reach another fleet's data.
    auth.enforce_not_agent_credential("reassign agent fleets")
    auth.enforce_not_org_member("reassign agent fleets")
    fleet_id = body.get("fleet_id")
    if not fleet_id:
        raise HTTPException(status_code=400, detail="fleet_id is required")

    sc = get_storage_client()
    agent = await lookup_agent(tenant_id, agent_id, read=False)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

    old_fleet = agent.get("fleet_id")
    stored_agent_id = agent["agent_id"]
    await sc.update_agent_fleet(stored_agent_id, {"tenant_id": tenant_id, "fleet_id": fleet_id})
    # A home-fleet move grants that fleet's own-fleet access, so it is audited
    # like a trust change.
    await log_action(
        tenant_id=tenant_id,
        action="agent_fleet_update",
        resource_type="agent",
        resource_id=agent.get("id"),
        detail={
            "agent_id": stored_agent_id,
            "old_fleet_id": old_fleet,
            "new_fleet_id": fleet_id,
            **auth.audit_actor(),
        },
    )
    return {"agent_id": stored_agent_id, "old_fleet_id": old_fleet, "new_fleet_id": fleet_id}


@router.get("/agents/{agent_id}/tune", response_model=AgentOut)
async def get_agent_tune(
    agent_id: str,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
):
    """Get an agent's current search profile (retrieval tuning parameters)."""
    auth.enforce_tenant(tenant_id)
    agent = await lookup_agent(tenant_id, agent_id)
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
    """Update an agent's search profile (per-agent retrieval tuning). Pass ?reset=true to clear.

    Auth: any write-capable credential for the tenant, agent-scoped included —
    self-tune is the point of the route. The two gates answer different
    questions: ``enforce_read_only`` whether this credential may write at all,
    the identity check below whose profile it may write.
    """
    # ``enforce_usage_limits`` is deliberately NOT applied, and pinned that way
    # by ``test_agent_tune_still_works_when_over_usage_limits``. The principle
    # is the one stated on ``WRITE_QUOTA_OPS`` in ``usage_service``: an update
    # that rewrites a row rather than adding one does not grow the store, and
    # this writes a single column on a row that must already exist. Lowering
    # ``top_k``/``graph_max_hops`` is also how an over-quota tenant reduces
    # retrieval cost — ``?reset=true`` is not part of that half, since it
    # restores defaults and a default can exceed the value it replaces.
    auth.enforce_read_only()
    auth.enforce_tenant(tenant_id)
    # An agent may tune ITS OWN profile (also exposed via MCP caura_tune), but
    # not a peer's — block cross-agent tamper while leaving self-tune + admin keys.
    auth.enforce_self_agent(agent_id, message="Agents can only tune their own search profile.")
    # M-123: that check passes a credential with no agent identity, and an
    # install credential has none on the wire. The install must already own the
    # agent: an unclaimed, missing or foreign one is refused (403, without saying
    # which). Not the write gate's first-touch leniency: MCP ``caura_tune`` gets
    # that through ``resolve_write_agent``, which claims the agent, but this
    # route claims nothing, so an unclaimed agent would stay tunable by every
    # install. Not degraded either: the agent is the resource this URL names.
    if auth.is_install_credential:
        await enforce_broker_agent_ownership(tenant_id, agent_id, auth.install_uuid)
    sc = get_storage_client()
    # ``read=False``: this row is not just inspected, it is MERGED INTO below —
    # ``current`` starts as the stored profile and only the supplied fields are
    # overwritten. Served from a replica under lag, the fields this request does
    # not mention are written back from a stale snapshot, quietly reverting a
    # tune that had already landed. A read that feeds a write belongs on the
    # primary for the same reason the re-fetches below do.
    agent = await lookup_agent(tenant_id, agent_id, read=False)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

    if reset:
        stored_agent_id = agent["agent_id"]
        cleared = await sc.reset_search_profile(stored_agent_id, tenant_id)
        if not cleared:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        await log_action(
            tenant_id=tenant_id,
            agent_id=auth.agent_id,
            action="agent_tune",
            resource_type="agent",
            resource_id=agent.get("id"),
            detail={"agent_id": stored_agent_id, "reset": True, **auth.audit_actor()},
        )
        # Storage answers the reset with ``{"ok": true}``, not the agent row, so
        # the response has to come from a re-read. The merge branch below
        # re-reads too but falls back to the stale pre-write row on a miss,
        # where this answers 404 — a reset must not hand back the profile it
        # just cleared.
        #
        # ``read=False`` is what makes that true. From a replica this re-read
        # can still see the pre-reset profile, which is exactly the value this
        # branch says it must never hand back — or miss the row entirely and
        # turn a successful reset into a 404.
        refreshed = await sc.get_agent(stored_agent_id, tenant_id, read=False)
        if not refreshed:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        return AgentOut.model_validate(refreshed)

    # Merge: only set non-None fields, preserve existing profile values
    current = agent.get("search_profile") or {}
    updates = body.model_dump(exclude_none=True)
    if updates:
        current.update(updates)
        current = validate_search_profile(current)
        await sc.update_search_profile(agent["id"], tenant_id, current)
        await log_action(
            tenant_id=tenant_id,
            agent_id=auth.agent_id,
            action="agent_tune",
            resource_type="agent",
            resource_id=agent.get("id"),
            detail={"agent_id": agent["agent_id"], "changes": updates, **auth.audit_actor()},
        )
        # Re-fetch to get the updated agent with full fields. ``read=False``
        # because this is a read-after-write: from a replica it can return the
        # profile as it was before the update on the line above, so the PATCH
        # would answer with the value it just replaced.
        refreshed = await sc.get_agent(agent["agent_id"], tenant_id, read=False)
        if refreshed:
            return AgentOut.model_validate(refreshed)
    return AgentOut.model_validate(agent)


@router.delete("/agents/{agent_id}", status_code=204)
async def delete_agent(
    agent_id: str,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
):
    """Delete an agent. Memories written by this agent are NOT deleted."""
    auth.enforce_read_only()
    auth.enforce_tenant(tenant_id)
    # Deleting an agent wipes its trust/profile/identity (and re-registration
    # resets to DEFAULT_TRUST_LEVEL) — an agent must not delete itself/peers to
    # evade controls.
    auth.enforce_not_agent_credential("delete agents")
    auth.enforce_not_org_member("delete agents")
    sc = get_storage_client()
    agent = await lookup_agent(tenant_id, agent_id, read=False)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
    await log_action(
        tenant_id=tenant_id,
        action="delete",
        resource_type="agent",
        detail={"agent_id": agent["agent_id"], "fleet_id": agent.get("fleet_id")},
    )
    await sc.delete_agent(agent["agent_id"], tenant_id)
