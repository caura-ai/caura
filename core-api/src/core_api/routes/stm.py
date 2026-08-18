"""STM (Short-Term Memory) REST endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from common.enrichment.constants import SERVER_RESERVED_MEMORY_TYPES
from core_api.auth import AuthContext, get_auth_context
from core_api.config import settings
from core_api.services.agent_service import enforce_fleet_write, resolve_write_agent
from core_api.services.usage_service import check_and_increment

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stm"])


def _reject_reserved_memory_type(memory_type: str | None) -> None:
    """Twin of ``memories._reject_reserved_memory_type``, for the promote door.

    Deliberately a small duplicate rather than an import of that module's
    private helper: importing across route modules to reach a ``_``-prefixed
    function invites a circular import for no real gain. The source of truth
    both share is ``SERVER_RESERVED_MEMORY_TYPES``, so the two cannot disagree
    about WHICH types are reserved — only about wording.
    """
    if memory_type is None or memory_type not in SERVER_RESERVED_MEMORY_TYPES:
        return
    raise HTTPException(
        status_code=422,
        detail=(f"memory_type='{memory_type}' is server-reserved and cannot be supplied on writes."),
    )


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
    # Authenticated agent identity (gateway X-Agent-ID) takes precedence over the
    # caller-supplied query param. Notes are per-agent PRIVATE (see the section
    # header), so an agent credential must not read a peer's by naming it.
    #
    # The DELETE twin directly below has enforced this since the 2026-06-11
    # audit, which left the pair lopsided: a peer's notes could not be cleared,
    # only read. Disclosure was the half still open.
    if auth.agent_id and agent_id != auth.agent_id:
        raise HTTPException(
            status_code=403,
            detail=f"agent_id '{agent_id}' does not match the authenticated agent identity.",
        )
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
    auth.enforce_read_only()
    tenant_id = _require_tenant(auth)
    # Authenticated agent identity (gateway X-Agent-ID) takes precedence —
    # an agent credential must not clear a peer agent's notes by naming it.
    if auth.agent_id and agent_id != auth.agent_id:
        raise HTTPException(
            status_code=403,
            detail=f"agent_id '{agent_id}' does not match the authenticated agent identity.",
        )
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
    auth.enforce_read_only()
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
):
    _check_stm_enabled()
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    tenant_id = _require_tenant(auth)
    # A promote IS an LTM write, so it owes the same gates as POST /memories.
    # It reached LTM having paid only the two above, which meant the STM door
    # into long-term memory was cheaper than the front door.
    _reject_reserved_memory_type(body.memory_type)
    # Bind the promoted memory to the authenticated agent identity when the
    # credential carries one — a caller must not promote into LTM on behalf
    # of an arbitrary peer agent.
    if auth.agent_id and body.agent_id != auth.agent_id:
        raise HTTPException(
            status_code=403,
            detail=f"agent_id '{body.agent_id}' does not match the authenticated agent identity.",
        )

    from core_api.services.organization_settings import resolve_config

    write_config = await resolve_config(tenant_id)
    # ``resolve_write_agent`` is what POST /memories uses, and it SUBSUMES the
    # bare ``broker_owned_agent_id`` call this replaced: it enforces the same
    # broker ownership boundary (degrading a foreign / reserved-namespace agent
    # id to the caller's own broker:<install> fallback) AND applies the
    # agent-approval policy, returning the agent row the trust gate below needs.
    # Calling both would have done the ownership work twice.
    agent, body.agent_id = await resolve_write_agent(
        body.agent_id,
        tenant_id,
        body.fleet_id,
        is_install_credential=auth.is_install_credential,
        install_uuid=auth.install_uuid,
        require_approval=write_config.require_agent_approval,
    )
    # A quarantined agent (trust_level 0) must not reach LTM by any door.
    if agent.get("trust_level", 0) == 0:
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{body.agent_id}' is not approved. Contact tenant admin to set trust_level >= 1.",
        )
    # Resolve fleet from the agent's home fleet, as the write path does, so the
    # fleet-write policy below is evaluated against the fleet the memory will
    # actually land in rather than against None.
    if not body.fleet_id and agent.get("fleet_id"):
        body.fleet_id = agent["fleet_id"]
    if auth.tenant_id:  # skip enforcement + metering for admin, as POST /memories does
        await enforce_fleet_write(tenant_id, body.agent_id, body.fleet_id)
        # Metering, distinct from ``enforce_usage_limits`` above: that one
        # refuses a write when the org is ALREADY over its plan cap, this one is
        # what makes the write count toward the cap. Without it a tenant could
        # promote without limit and never trip the check.
        await check_and_increment(tenant_id, "write")

    from core_api.services.stm_service import promote

    result = await promote(
        content=body.content,
        tenant_id=tenant_id,
        agent_id=body.agent_id,
        fleet_id=body.fleet_id,
        memory_type=body.memory_type,
        visibility=body.visibility,
    )
    return result
