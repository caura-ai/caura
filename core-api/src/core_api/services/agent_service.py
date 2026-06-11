"""Agent trust-level enforcement for fleet-scoped access control."""

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core_api.clients.storage_client import get_storage_client
from core_api.constants import DEFAULT_TRUST_LEVEL
from core_api.services.audit_service import log_action

logger = logging.getLogger(__name__)


async def get_or_create_agent(
    db: AsyncSession,
    tenant_id: str,
    agent_id: str,
    fleet_id: str | None = None,
    *,
    require_approval: bool = False,
    display_name: str | None = None,
    install_id: str | None = None,
) -> dict:
    """Return the agent dict, creating it on first encounter.

    The storage API handles upsert semantics and race-condition safety.

    ``display_name`` and ``install_id`` (Task 6) are accepted optionally
    on every call. On creation they're persisted; on lookup of an
    existing row, ``display_name`` is refreshed if the new value
    differs (so a renamed machine propagates) and ``install_id`` is
    backfilled when previously NULL but never overwritten — the
    install identity is stable for the row's lifetime.
    """
    sc = get_storage_client()
    agent = await sc.get_agent(agent_id, tenant_id)
    if agent:
        # Backfill fleet_id if the agent was registered without one,
        # refresh display_name when it differs (hostname change), and
        # stamp install_id on first contact post-plugin-upgrade.
        backfill: dict = {}
        if agent.get("fleet_id") is None and fleet_id is not None:
            backfill["fleet_id"] = fleet_id
        if display_name is not None and agent.get("display_name") != display_name:
            backfill["display_name"] = display_name
        if install_id is not None and agent.get("install_id") is None:
            backfill["install_id"] = install_id
        if backfill:
            agent.update(backfill)
            agent["updated_at"] = datetime.now(UTC)
            await sc.create_or_update_agent({"tenant_id": tenant_id, "agent_id": agent_id, **backfill})
        return agent

    # Legacy-main carryover: pre-Task6 plugins all defaulted to
    # ``agent_id="main"``, so an upgrade from those creates a brand-new
    # ``main-{install_id}`` row and orphans the old "main" row's
    # tuning state. When this is a fresh ``main-{install_id}`` create
    # for a tenant/fleet that has a legacy "main" row, copy
    # ``trust_level`` and ``search_profile`` forward so the upgraded
    # plugin keeps the operator's prior calibration. Bounded scope:
    #   - only triggers for ``main-{install_id}`` ids (not arbitrary
    #     custom agents) so a deliberate new agent doesn't accidentally
    #     inherit
    #   - skipped when ``require_approval=True`` (the explicit
    #     "start at 0" path)
    #   - leaves the legacy row intact so its memories stay queryable
    #     under ``agent_id="main"`` for admin recovery; operators
    #     decide later whether to delete or keep as archive
    inherited_trust: int | None = None
    inherited_search_profile: dict[str, Any] | None = None
    if not require_approval and install_id is not None and agent_id == f"main-{install_id}":
        legacy = await sc.get_agent("main", tenant_id)
        if legacy and (fleet_id is None or legacy.get("fleet_id") == fleet_id):
            inherited_trust = legacy.get("trust_level")
            inherited_search_profile = legacy.get("search_profile")
            logger.info(
                "carrying forward legacy 'main' agent state to install-scoped id",
                extra={
                    "tenant_id": tenant_id,
                    "fleet_id": fleet_id,
                    "new_agent_id": agent_id,
                    "inherited_trust": inherited_trust,
                },
            )

    initial_trust = (
        inherited_trust if inherited_trust is not None else (0 if require_approval else DEFAULT_TRUST_LEVEL)
    )
    create_payload: dict[str, Any] = {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "fleet_id": fleet_id,
        "display_name": display_name,
        "install_id": install_id,
        "trust_level": initial_trust,
    }
    if inherited_search_profile is not None:
        create_payload["search_profile"] = inherited_search_profile
    agent = await sc.create_or_update_agent(create_payload)
    await log_action(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        action="agent_registered",
        resource_type="agent",
        resource_id=agent.get("id"),
        detail={
            "fleet_id": fleet_id,
            "trust_level": initial_trust,
            "display_name": display_name,
            "install_id": install_id,
            "carried_from_legacy_main": inherited_trust is not None,
        },
    )
    return agent


async def lookup_agent(db: AsyncSession, tenant_id: str, agent_id: str) -> dict | None:
    sc = get_storage_client()
    return await sc.get_agent(agent_id, tenant_id)


async def enforce_fleet_write(
    db: AsyncSession,
    tenant_id: str,
    agent_id: str,
    fleet_id: str | None,
) -> dict:
    """Enforce write permissions. Returns the agent (auto-created if new)."""
    agent = await get_or_create_agent(db, tenant_id, agent_id, fleet_id)

    # Agents can always write to their home fleet (or tenant-wide if no fleet specified)
    if fleet_id is None or fleet_id == agent.get("fleet_id"):
        return agent

    # Cross-fleet write requires admin (level >= 3)
    trust = agent.get("trust_level", 0)
    if trust < 3:
        raise HTTPException(
            status_code=403,
            detail=f"fleet-scope policy: fleet '{fleet_id}' is not writable by principals of fleet '{agent.get('fleet_id') or 'none'}'.",
        )
    return agent


async def enforce_fleet_read(
    db: AsyncSession,
    tenant_id: str,
    agent_id: str,
    fleet_id: str | None,
) -> None:
    """Enforce read permissions for search/list (read-only — never creates agents)."""
    agent = await lookup_agent(db, tenant_id, agent_id)

    # Unknown agent — allow the read (agent registration happens on writes)
    if not agent:
        return

    # Reading own fleet or tenant-wide is always allowed
    if fleet_id is None or fleet_id == agent.get("fleet_id"):
        return

    # Cross-fleet read requires level >= 2
    trust = agent.get("trust_level", 0)
    if trust < 2:
        raise HTTPException(
            status_code=403,
            detail=f"fleet-scope policy: fleet '{fleet_id}' is not readable by principals of fleet '{agent.get('fleet_id') or 'none'}'.",
        )


async def authorize_memory_access(
    db: AsyncSession,
    tenant_id: str,
    caller_agent_id: str | None,
    *,
    visibility: str | None,
    owner_agent_id: str | None,
    fleet_id: str | None,
    write: bool = False,
) -> bool:
    """Authorize a *by-id* memory access against the fleet/scope contract.

    By-id handlers (``GET/PATCH/DELETE /memories/{id}`` and the MCP
    ``read``/``lineage``/``transition``/``update``/``delete`` ops) historically
    authorized on ``tenant_id`` alone, while the list/search paths additionally
    enforce ``scope_agent`` ownership (``memory_repository`` visibility
    predicate) and the cross-fleet trust ladder (``enforce_fleet_read``). That
    asymmetry let any same-tenant agent credential read or mutate a peer's
    fleet/agent-scoped row by id (BOLA/IDOR). This helper restores parity so
    every surface enforces the same contract.

    Returns ``True`` if ``caller_agent_id`` may access the row.

    - ``caller_agent_id is None`` → a tenant-scoped user/dashboard credential
      (no gateway ``X-Agent-ID``) → full tenant access, unchanged. The agent
      isolation boundary only applies to agent-scoped credentials.
    - ``scope_agent`` → author-only.
    - ``scope_org`` → tenant-global (mirrors ``scored_search``'s rule that
      org-scoped rows escape fleet scoping).
    - ``scope_team`` / default → fleet-gated: own fleet (or fleet-less rows)
      always; cross-fleet requires ``trust_level >= 2`` for reads, ``>= 3`` for
      writes (mirrors ``enforce_fleet_read`` / ``enforce_fleet_write``).
    """
    if not caller_agent_id:
        return True
    if visibility in ("scope_agent", "scope_org"):
        # No agent row needed for these branches.
        return memory_access_allowed_for_agent(
            None,
            caller_agent_id,
            visibility=visibility,
            owner_agent_id=owner_agent_id,
            fleet_id=fleet_id,
            write=write,
        )
    # scope_team / unknown visibility: fleet-gated by the trust ladder.
    agent = await lookup_agent(db, tenant_id, caller_agent_id)
    return memory_access_allowed_for_agent(
        agent,
        caller_agent_id,
        visibility=visibility,
        owner_agent_id=owner_agent_id,
        fleet_id=fleet_id,
        write=write,
    )


def memory_access_allowed_for_agent(
    agent: dict | None,
    caller_agent_id: str,
    *,
    visibility: str | None,
    owner_agent_id: str | None,
    fleet_id: str | None,
    write: bool = False,
) -> bool:
    """Pure predicate behind :func:`authorize_memory_access`.

    Takes the caller's pre-fetched agent row so loops over many rows
    (e.g. an entity's linked memories / relations) resolve the agent once
    instead of issuing one identical lookup per row (N+1). ``agent=None``
    on the scope_team branch means the identity is unregistered — mirror
    ``enforce_fleet_read``'s allow-on-unknown (registration happens on
    writes; reads of an unregistered identity are not the isolation
    boundary this helper guards).
    """
    if visibility == "scope_agent":
        return owner_agent_id == caller_agent_id
    if visibility == "scope_org":
        return True
    if not agent:
        return True
    if fleet_id is None or fleet_id == agent.get("fleet_id"):
        return True
    return agent.get("trust_level", 0) >= (3 if write else 2)


async def enforce_memory_read(
    db: AsyncSession,
    tenant_id: str,
    caller_agent_id: str | None,
    memory: Any,
) -> None:
    """Raise 404 if ``caller_agent_id`` may not read ``memory`` (an ORM row).

    404 (not 403) is deliberate: it mirrors the list/search contract where an
    out-of-scope row simply does not appear, and avoids confirming the
    existence of another fleet's/agent's memory_id to an unauthorized caller.
    """
    allowed = await authorize_memory_access(
        db,
        tenant_id,
        caller_agent_id,
        visibility=getattr(memory, "visibility", None),
        owner_agent_id=getattr(memory, "agent_id", None),
        fleet_id=getattr(memory, "fleet_id", None),
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="Memory not found")


async def enforce_delete(
    db: AsyncSession,
    tenant_id: str,
    agent_id: str,
) -> None:
    """Enforce delete permissions."""
    agent = await lookup_agent(db, tenant_id, agent_id)
    if not agent:
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{agent_id}' is not registered and cannot delete memories.",
        )

    trust = agent.get("trust_level", 0)
    if trust < 3:
        raise HTTPException(
            status_code=403,
            detail=f"access policy: principals of fleet '{agent.get('fleet_id') or 'none'}' are not permitted to delete memories.",
        )


async def enforce_update(
    db: AsyncSession,
    tenant_id: str,
    agent_id: str,
    memory_owner_agent_id: str,
) -> None:
    """Enforce update permissions. Level 0-2 can only update own memories; level 3 can update any."""
    agent = await lookup_agent(db, tenant_id, agent_id)
    if not agent:
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{agent_id}' is not registered and cannot update memories.",
        )
    trust = agent.get("trust_level", 0)
    if trust == 0:
        raise HTTPException(
            status_code=403,
            detail=f"access policy: agent '{agent_id}' is restricted from updates.",
        )
    if trust < 3 and agent_id != memory_owner_agent_id:
        raise HTTPException(
            status_code=403,
            detail=f"access policy: agent '{agent_id}' may only update its own memories.",
        )


async def backfill_agents(db: AsyncSession) -> int:
    """Create agent rows for any (tenant_id, agent_id) pairs in memories that don't have one yet."""
    sc = get_storage_client()
    # Use the first available tenant_id — in standalone mode there's only one
    from core_api.standalone import get_standalone_tenant_id

    tenant_id = get_standalone_tenant_id()
    result = await sc.backfill_from_memories(tenant_id)
    return result.get("count", 0)


async def update_trust_level(
    db: AsyncSession,
    tenant_id: str,
    agent_id: str,
    trust_level: int,
    fleet_id: str | None = None,
) -> dict:
    """Update an agent's trust level (and optionally fleet). Returns the updated agent."""
    agent = await lookup_agent(db, tenant_id, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

    sc = get_storage_client()
    data: dict[str, Any] = {"tenant_id": tenant_id, "trust_level": trust_level}
    if fleet_id is not None:
        data["fleet_id"] = fleet_id
    await sc.update_trust_level(agent_id, data)
    # Re-fetch to get the updated agent dict
    updated = await sc.get_agent(agent_id, tenant_id)
    return updated or agent
