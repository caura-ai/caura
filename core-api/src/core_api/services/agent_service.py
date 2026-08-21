"""Agent trust-level enforcement for fleet-scoped access control."""

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from core_api.clients.storage_client import get_storage_client
from core_api.constants import DEFAULT_TRUST_LEVEL
from core_api.services.audit_service import log_action

logger = logging.getLogger(__name__)


async def get_or_create_agent(
    tenant_id: str,
    agent_id: str,
    fleet_id: str | None = None,
    *,
    require_approval: bool = False,
    display_name: str | None = None,
    install_id: str | None = None,
    owner_install_uuid: str | None = None,
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
        if owner_install_uuid is not None and agent.get("owner_install_uuid") is None:
            backfill["owner_install_uuid"] = owner_install_uuid
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
        "owner_install_uuid": owner_install_uuid,
        "trust_level": initial_trust,
    }
    if inherited_search_profile is not None:
        create_payload["search_profile"] = inherited_search_profile
    agent = await sc.create_or_update_agent(create_payload)
    await log_action(
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
            "owner_install_uuid": owner_install_uuid,
            "carried_from_legacy_main": inherited_trust is not None,
        },
    )
    return agent


async def lookup_agent(tenant_id: str, agent_id: str) -> dict | None:
    sc = get_storage_client()
    return await sc.get_agent(agent_id, tenant_id)


_BROKER_LABEL_PREFIX = "broker:"


def broker_label(install_uuid: str | None) -> str:
    """The bare-install fallback identity for a broker write."""
    return f"{_BROKER_LABEL_PREFIX}{install_uuid or 'unknown'}"


def _owned_by_other_install(owner_install_uuid: str | None, install_uuid: str | None) -> bool:
    """True when an agent row is first-touch owned by a DIFFERENT install than
    ``install_uuid`` — the single condition under which a broker write is
    degraded to its own ``broker:<install>`` identity. A NULL owner (unclaimed
    or grandfathered) is not "another install", so it never triggers a degrade.
    """
    return owner_install_uuid is not None and owner_install_uuid != install_uuid


async def broker_owned_agent_id(chosen: str, install_uuid: str | None, tenant_id: str) -> str:
    """Lenient ownership gate over a broker write's chosen agent id.

    A broker write may be attributed to an agent named by the caller (REST item
    metadata / body.agent_id, or the MCP agent id). Before trusting that name,
    verify this install owns it: ``owner_install_uuid`` is stamped (first-touch)
    with the install that first wrote as the agent. Degrade to the bare-install
    identity ONLY when the named agent is owned by a *different* install, so one
    install can't write under another install's agent id.

    Lenient — never blocks a legitimate first write:
      - ``install_uuid`` is None        -> keep (no identity to enforce against)
      - already the install fallback   -> no lookup, return as-is
      - another install's broker:<x>   -> degrade (reserved namespace; no lookup)
      - agent doesn't exist yet         -> keep (this write first-touches it)
      - ``owner_install_uuid`` is NULL  -> keep (unclaimed; this write claims it)
      - owned by THIS install           -> keep
      - owned by a DIFFERENT install    -> degrade to ``broker:<install>``
    """
    # No install identity means there is nothing to enforce ownership against:
    # the gateway couples ``x-caura-credential-kind`` with ``x-install-uuid`` behind
    # its shared-secret perimeter (auth.py Path 4), so a set credential kind without
    # a uuid is a contract violation, never the real adversary (a *different* install
    # always carries its own uuid and is still gated below; forging this state
    # requires the gateway secret, which already permits direct x-agent-id spoofing).
    # Fall through UNGATED — write as named, as before the boundary — rather than
    # pool every such caller onto the shared ``broker:unknown`` id. Log it: the state
    # should never occur in prod, so surface it in telemetry without blocking.
    if install_uuid is None:
        logger.warning(
            "broker write carried an install credential with no install_uuid; "
            "writing agent_id=%s as-named ungated (tenant=%s)",
            chosen,
            tenant_id,
        )
        return chosen
    fallback = broker_label(install_uuid)
    if chosen == fallback:
        return chosen
    # The ``broker:<install>`` namespace is RESERVED — an install may only ever
    # write as its OWN bare-install identity. A chosen id in that namespace that
    # isn't this install's own fallback (handled above) is *another* install's
    # reserved identity. Degrade to this install's own fallback, with NO lookup:
    # the fallback id is deterministic and guessable, so without this guard an
    # attacker could first-touch ``broker:<victim>`` (stamping itself as owner)
    # and thereby capture the victim's later degraded writes.
    if chosen.startswith(_BROKER_LABEL_PREFIX):
        return fallback
    owner = await lookup_agent(tenant_id, chosen)
    if owner is None:
        return chosen
    if _owned_by_other_install(owner.get("owner_install_uuid"), install_uuid):
        return fallback
    return chosen


async def resolve_write_agent(
    chosen_agent_id: str,
    tenant_id: str,
    fleet_id: str | None,
    *,
    is_install_credential: bool,
    install_uuid: str | None,
    require_approval: bool = False,
) -> tuple[dict, str]:
    """Resolve the agent a write is attributed to, enforcing the broker
    ownership boundary, and return ``(agent_row, safe_agent_id)``.

    Shared by every write entry point — the REST single- and bulk-write paths
    and the MCP write tool — so the boundary can't be bypassed by a caller's
    choice of endpoint. For a broker (install-credential) caller it:

      1. Gates ``chosen_agent_id`` through :func:`broker_owned_agent_id`
         (degrade to ``broker:<install>`` when it names an agent owned by a
         different install, or another install's reserved ``broker:`` id).
      2. Stamps ``owner_install_uuid`` first-touch via ``get_or_create_agent``.
      3. Re-checks the committed row and degrades the loser of a first-touch
         race to its own ``broker:<install>`` identity — the row is now
         authoritative (storage re-selects FOR UPDATE and never overwrites a
         set owner), so this closes the gate's optimistic ``owner is None``
         window.

    Non-broker callers (dashboard / SDK / interactive MCP) pass straight through
    ``get_or_create_agent`` unchanged — the stamp and gate are broker-only, keyed
    on ``is_install_credential`` (a stray ``install_uuid`` without the credential
    kind is ignored).
    """
    if is_install_credential:
        chosen_agent_id = await broker_owned_agent_id(chosen_agent_id, install_uuid, tenant_id)
    agent = await get_or_create_agent(
        tenant_id,
        chosen_agent_id,
        fleet_id,
        require_approval=require_approval,
        # Stamp ownership only for broker writes — never rely on the gateway
        # happening to omit x-install-uuid for non-broker callers.
        owner_install_uuid=install_uuid if is_install_credential else None,
    )
    if (
        is_install_credential
        and install_uuid
        and _owned_by_other_install(agent.get("owner_install_uuid"), install_uuid)
    ):
        chosen_agent_id = broker_label(install_uuid)
        agent = await get_or_create_agent(
            tenant_id,
            chosen_agent_id,
            fleet_id,
            require_approval=require_approval,
            owner_install_uuid=install_uuid,
        )
    return agent, chosen_agent_id


async def enforce_fleet_write(
    tenant_id: str,
    agent_id: str,
    fleet_id: str | None,
) -> dict:
    """Enforce write permissions. Returns the agent (auto-created if new)."""
    agent = await get_or_create_agent(tenant_id, agent_id, fleet_id)

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
    tenant_id: str,
    agent_id: str,
    fleet_id: str | None,
) -> None:
    """Enforce read permissions for search/list (read-only — never creates agents)."""
    agent = await lookup_agent(tenant_id, agent_id)

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


async def resolve_read_fleet_gate(
    tenant_id: str,
    agent_id: str,
    scope: str,
    fleet_id: str | None,
) -> tuple[int, str | None]:
    """Resolve ``(min_trust, effective_fleet_id)`` for the scoped read-enumeration
    surfaces: the MCP tools ``caura_list`` / ``caura_stats`` and the REST routes
    ``GET /memories`` / ``GET /memories/stats`` when they are called with an
    explicit ``scope``. All four share this one implementation so the ladder
    cannot drift between the surfaces.

    Trust ladder per the product spec: level 1 = read within the caller's OWN
    fleet; level 2 = cross-fleet read. The requirement therefore keys off the
    TARGET, not the ``scope`` string alone:

    * ``scope='agent'`` → ``(1, fleet_id)`` — own memories (caller-filtered
      upstream); no agent lookup needed.
    * ``scope='all'``   → ``(2, fleet_id)`` — spans fleets by definition, so it
      is always a cross-fleet read.
    * ``scope='fleet'`` → the caller's own fleet is level 1; a DIFFERENT
      explicit ``fleet_id`` is level 2. Mirrors ``enforce_fleet_read`` and the
      ``caura_recall`` gate so all read surfaces agree.

    For ``scope='fleet'`` with no ``fleet_id`` this is a security decision, not
    a convenience: ``memory_list_by_filters`` / ``memory_stats_breakdown`` only
    apply the ``fleet_id`` filter when it is set, and their visibility predicate
    returns ``scope_team`` rows across ALL fleets — so an unfiltered level-1 read
    would fan out to other fleets' shared rows. A constrained caller (trust < 2)
    that omits ``fleet_id`` is therefore resolved by three cases:

    (a) has a home fleet          → PIN ``fleet_id`` to it (own-fleet read, L1);
    (b) registered but fleet-less → force L2 — the caller can't prove membership
        in any fleet, so ``require_trust`` rejects it (trust 1 < 2) rather than
        granting an unfiltered scan;
    (c) unregistered (no row)     → soft-pass at L1 with no pin, matching the
        read-ergonomics soft-pass in ``require_trust`` / ``caura_recall``.

    A trust ≥ 2 caller is never pinned (it is allowed cross-fleet reads). With
    an explicit fleet it resolves as above; with NO ``fleet_id`` the query would
    fan out across all fleets, so this returns ``(2, fleet_id)`` — the caller
    clears L2, while a caller demoted below 2 between this read and the
    ``require_trust`` re-read does not.
    """
    if scope == "agent":
        return 1, fleet_id
    if scope == "all":
        return 2, fleet_id
    # scope == "fleet": decide by target fleet vs the caller's home fleet.
    agent = await get_storage_client().get_agent(agent_id, tenant_id)
    home_fleet = (agent or {}).get("fleet_id")
    # trust_level pre-read from the agent storage row (agents.trust_level is the
    # single source of truth; update_trust_level writes it directly). Used to:
    #   1. Pick the pin branch (docstring a/b): trust < 2 pins to the home fleet,
    #      or forces L2 when the caller is fleet-less.
    #   2. Pick the fan-out branch: trust >= 2 with no fleet_id returns L2, so the
    #      unfiltered cross-fleet scan is gated at the level it actually needs.
    # Every branch returns a min_level that MATCHES the access it grants, so a
    # stale read can only UNDER-grant (a narrower result), never over-grant:
    # require_trust re-reads trust and rejects a caller demoted below the
    # returned bar. get_agent is uncached, keeping even that transient divergence
    # to a single async context switch (do NOT add caching — it would widen it).
    trust = (agent or {}).get("trust_level", DEFAULT_TRUST_LEVEL)
    # A different fleet is cross-fleet (L2). An unregistered / fleet-less caller
    # (no home_fleet) that names an explicit fleet also can't prove ownership,
    # so it must clear the L2 gate too — require_trust then rejects an
    # unregistered id (effective trust 1 < 2) rather than granting an
    # own-fleet pass for a fleet the caller has no established membership in.
    if fleet_id and (not home_fleet or fleet_id != home_fleet):
        return 2, fleet_id  # cross-fleet read → level 2
    if not fleet_id and trust < 2:
        if home_fleet:
            fleet_id = home_fleet  # (a) pin to own fleet → L1
        elif agent is not None:
            return 2, fleet_id  # (b) registered fleet-less → L2 (require_trust rejects)
        # (c) unregistered soft-pass → falls through to (1, None).
    elif not fleet_id:
        # trust >= 2, no pin requested — but fleet_id=None means the downstream
        # query fans out across all fleets (scope_team rows). Return L2 so
        # require_trust enforces the correct bar and eliminates the TOCTOU
        # window: a still-trust-2 caller passes L2; a race-demoted trust-1
        # caller does not.
        return 2, fleet_id
    return 1, fleet_id


async def authorize_memory_access(
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
    agent = await lookup_agent(tenant_id, caller_agent_id)
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
        tenant_id,
        caller_agent_id,
        visibility=getattr(memory, "visibility", None),
        owner_agent_id=getattr(memory, "agent_id", None),
        fleet_id=getattr(memory, "fleet_id", None),
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="Memory not found")


async def enforce_delete(
    tenant_id: str,
    agent_id: str,
) -> None:
    """Enforce delete permissions."""
    agent = await lookup_agent(tenant_id, agent_id)
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
    tenant_id: str,
    agent_id: str,
    memory_owner_agent_id: str,
) -> None:
    """Enforce update permissions. Level 0-2 can only update own memories; level 3 can update any."""
    agent = await lookup_agent(tenant_id, agent_id)
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


async def backfill_agents() -> int:
    """Create agent rows for any (tenant_id, agent_id) pairs in memories that
    don't have one yet. Fully storage-routed (one ``sc.backfill_from_memories``
    call) — no DB session needed.
    """
    sc = get_storage_client()
    # Use the first available tenant_id — in standalone mode there's only one
    from core_api.standalone import get_standalone_tenant_id

    tenant_id = get_standalone_tenant_id()
    result = await sc.backfill_from_memories(tenant_id)
    return result.get("count", 0)


async def update_trust_level(
    tenant_id: str,
    agent_id: str,
    trust_level: int,
    fleet_id: str | None = None,
) -> dict:
    """Update an agent's trust level (and optionally fleet). Returns the updated agent.

    Invariant: ``agents.trust_level`` is the SINGLE source of truth for agent
    trust. There is no separate trust cache/service to keep in sync — this
    writes the column directly (via ``sc.update_trust_level``) and every gate
    (``require_trust``) and the fleet-pin decision in
    ``mcp_server._resolve_read_fleet_gate`` reads that same column live through
    an uncached ``get_agent``. Do not introduce a second trust store; if one is
    ever added, this write must become a write-through to keep them consistent.
    """
    agent = await lookup_agent(tenant_id, agent_id)
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
