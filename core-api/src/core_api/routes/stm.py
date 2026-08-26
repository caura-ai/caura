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

# CAP-01 / F6 — surface honesty for STM.
#
# STM is advertised here and reachable nowhere. Three facts, all verified
# against a running stack:
#
#   1. There is no REST WRITE route. POST /stm/notes and POST /stm/bulletin
#      do not exist, so a caller who reads these docs and tries to put
#      anything into short-term memory gets a bare 405 with no explanation.
#      Writing is plugin-only.
#   2. Every read, delete and promote below is gated on ``USE_STM``, which is
#      off by default and is not tenant-toggleable — the setting appears
#      nowhere in the hosted deployment, so a hosted customer cannot turn it
#      on at any price.
#   3. The gate's own message used to say "Set USE_STM=true to enable
#      short-term memory", which is advice the reader it reaches cannot act
#      on. Telling someone to flip a switch they cannot reach is worse than
#      saying nothing.
#
# This block LABELS that. It does not enable STM, add the missing write
# routes, or change any status code — STM stays dead by standing decision, and
# A25 stays closed. The register's wording is the whole scope: label it, don't
# build it.
_PLUGIN_ONLY = (
    "**Plugin-only — not available over hosted REST.** Short-term memory is "
    "served by the OpenClaw plugin, not by this API. This operation is gated "
    "on the server-side `USE_STM` setting, which is off in the hosted "
    "deployment and cannot be enabled per tenant; it returns 422 there. "
    "There is also no REST write route for STM (`POST /stm/notes` and "
    "`POST /stm/bulletin` return 405), so nothing can be put into short-term "
    "memory over REST even where reads are enabled. Self-hosted operators who "
    "set `USE_STM=true` get the read, clear and promote operations only."
)


# Published description for the ``tenant_id`` selector every STM operation
# now accepts. STM has no markdown page of its own — STM is dead by standing
# decision and CAP-01 was a labelling task, so writing one would advertise it
# further — which makes the OpenAPI parameter description the only place a
# caller reads this. Say the whole rule there, including which credential MUST
# pass it and what omitting it costs.
#
# It deliberately does NOT name the env var behind the admin credential. The
# capability (an admin credential reaches any tenant it names) is inherent to
# the design and belongs in the spec; the server-side setting that grants it is
# operator configuration no API caller can act on, and this spec is published.
_TENANT_SELECTOR = (
    "Tenant to act on. A tenant-scoped credential should omit it — the "
    "credential's own tenant is used; echoing the same value is accepted and "
    "naming a DIFFERENT one is `403 TENANT_MISMATCH`. An **admin credential** "
    "carries no tenant of its own, so for admin callers this parameter is "
    'REQUIRED: omitting it returns **400** ("admin credential must name a '
    'tenant") — the credential is authenticated, so it is never a 401. A '
    "caller with neither a tenant nor admin rights still gets 401."
)


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
    """Reject with a message the reader can actually act on.

    The status stays 422 deliberately. It is arguably the wrong class — the
    caller's arguments are fine, the capability is absent — but the code that
    rides on it (``INVALID_ARGUMENTS``, derived from the status by
    ``errors.STATUS_TO_CODE``) is part of the wire contract, and re-classing it
    belongs with the error-design work in API-05 rather than here. What is in
    scope is the message, which told hosted callers to set a variable they have
    no way to reach.
    """
    if not settings.use_stm:
        raise HTTPException(
            status_code=422,
            detail=(
                "Short-term memory is not available on this deployment. STM is "
                "plugin-only: it is served by the OpenClaw plugin, and the hosted "
                "REST API cannot enable it (USE_STM is a server setting, not a "
                "per-tenant one). Self-hosted operators can set USE_STM=true; "
                "hosted callers should use the durable memory endpoints "
                "(/memories, /search) instead."
            ),
        )


def _require_tenant(auth: AuthContext, explicit_tenant_id: str | None = None) -> str:
    """Twin of ``skills_inbox._require_tenant`` — the other half of WT-4.

    Every STM endpoint needs a concrete tenant. ``AuthContext.tenant_id``
    is typed ``str | None`` because some bootstrap paths land there
    pre-auth — but so does the OSS admin path (auth Path 1), which
    deliberately builds ``AuthContext(tenant_id=None, is_admin=True)``.
    Treating BOTH as "missing tenant → 401" told the most privileged
    credential it did not authenticate. #987 fixed that on the
    skills-inbox routes and left this copy of the same helper out of
    scope; this closes it.

    Resolution order (identical to the inbox twin):

    - Tenant-scoped credential (``auth.tenant_id`` set): the key's own
      tenant wins. A conflicting explicit ``?tenant_id=`` is a 403 —
      a tenant key must not act on another tenant.
    - Admin credential (no tenant of its own): acts on the tenant it
      names via ``?tenant_id=``. Naming none is a 400 (a REQUEST
      problem — the credential IS authenticated, so never 401).
    - Neither: genuinely unauthenticated bootstrap context → 401.

    The mismatch comparison happens BEFORE any lookup, on the two
    strings alone, so a caller cannot use the 403/404 split to learn
    whether a named tenant exists.

    Returning the narrowed ``str`` lets mypy verify the downstream
    calls without litter ``cast``s.
    """
    if auth.tenant_id:
        if explicit_tenant_id is not None and explicit_tenant_id != auth.tenant_id:
            # Neither id appears in the message, deliberately. The
            # credential's own tenant is a binding its holder was not
            # necessarily told (embedded and shared keys), so echoing it
            # discloses it to anyone holding the key; the requested tenant
            # is caller-controlled input, and reflecting it into a body
            # that also lands in logs is a log-injection surface (and a
            # stored-XSS one wherever an operator UI renders a detail
            # string). ``!r`` quotes, it does not sanitize. Neither value
            # is actionable anyway — clients branch on the machine-readable
            # ``TENANT_MISMATCH`` prefix, not on the prose.
            raise HTTPException(
                status_code=403,
                detail="TENANT_MISMATCH — this credential is not scoped to the requested tenant.",
            )
        return auth.tenant_id
    if getattr(auth, "is_admin", False):
        if explicit_tenant_id:
            return explicit_tenant_id
        raise HTTPException(
            status_code=400,
            detail="admin credential must name a tenant — pass ?tenant_id=",
        )
    raise HTTPException(
        status_code=401,
        detail="UNAUTHENTICATED — auth context has no tenant_id",
    )


# ---------------------------------------------------------------------------
# Notes (per-agent private)
# ---------------------------------------------------------------------------


@router.get("/stm/notes", description=_PLUGIN_ONLY)
async def get_notes(
    auth: AuthContext = Depends(get_auth_context),
    agent_id: str = Query(...),
    # Tenant selector for ADMIN credentials (auth Path 1 carries no
    # tenant of its own — see _require_tenant / WT-4). Tenant-scoped
    # keys may omit it (their own tenant wins) or echo it; a
    # conflicting value is a 403. Notes are named by ``agent_id``
    # alone, so there was no other way for an admin to say WHOSE.
    tenant_id: str | None = Query(None, description=_TENANT_SELECTOR),
    limit: int = Query(default=50, ge=1, le=200),
):
    _check_stm_enabled()
    tenant_id = _require_tenant(auth, tenant_id)
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


@router.delete("/stm/notes", description=_PLUGIN_ONLY)
async def clear_notes(
    auth: AuthContext = Depends(get_auth_context),
    agent_id: str = Query(...),
    # Tenant selector for admin credentials — see get_notes / WT-4.
    tenant_id: str | None = Query(None, description=_TENANT_SELECTOR),
):
    _check_stm_enabled()
    auth.enforce_read_only()
    tenant_id = _require_tenant(auth, tenant_id)
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


@router.get("/stm/bulletin", description=_PLUGIN_ONLY)
async def get_bulletin(
    auth: AuthContext = Depends(get_auth_context),
    fleet_id: str = Query(...),
    # Tenant selector for admin credentials — see get_notes / WT-4.
    # ``fleet_id`` names a fleet, not a tenant, and fleet ids are only
    # unique WITHIN a tenant, so it cannot stand in for one.
    tenant_id: str | None = Query(None, description=_TENANT_SELECTOR),
    limit: int = Query(default=100, ge=1, le=500),
):
    _check_stm_enabled()
    tenant_id = _require_tenant(auth, tenant_id)
    from core_api.services.stm_service import read_bulletin

    entries = await read_bulletin(tenant_id, fleet_id, limit=limit)
    return {
        "tenant_id": tenant_id,
        "fleet_id": fleet_id,
        "count": len(entries),
        "bulletin": entries,
    }


@router.delete("/stm/bulletin", description=_PLUGIN_ONLY)
async def clear_bulletin(
    auth: AuthContext = Depends(get_auth_context),
    fleet_id: str = Query(...),
    # Tenant selector for admin credentials — see get_notes / WT-4.
    tenant_id: str | None = Query(None, description=_TENANT_SELECTOR),
):
    _check_stm_enabled()
    auth.enforce_read_only()
    tenant_id = _require_tenant(auth, tenant_id)
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


@router.post("/stm/promote", description=_PLUGIN_ONLY)
async def promote_stm(
    body: PromoteRequest,
    auth: AuthContext = Depends(get_auth_context),
    # Tenant selector for admin credentials — see get_notes / WT-4.
    # A query param rather than a ``PromoteRequest`` field on purpose:
    # the body has no tenant_id today, and adding one would start
    # honouring the value ``StandaloneTenantMiddleware`` already
    # injects into every JSON body (pydantic drops it as an unknown
    # field right now). Query keeps the selector identical across all
    # five STM endpoints and leaves the request schema alone.
    tenant_id: str | None = Query(None, description=_TENANT_SELECTOR),
):
    _check_stm_enabled()
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    tenant_id = _require_tenant(auth, tenant_id)
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
    # Skip enforcement + metering for admin, as POST /memories does. Note this
    # branch was DEAD before the WT-4 fix above: an admin credential (tenant_id
    # None) never got past ``_require_tenant``. It is deliberately still keyed
    # on ``auth.tenant_id`` and not on the resolved ``tenant_id``, which is now
    # set for admins too.
    if auth.tenant_id:
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
