"""Keystone rules — REST surface (CAURA-000).

Public mirror of the ``memclaw_keystones`` / ``memclaw_keystones_set``
MCP tools. Thin proxy over core-storage's ``/api/v1/storage/keystones``;
trust enforcement (≥1 to author) and audit live in core-api so the
storage layer can stay a dumb CRUD service.

Endpoints (under ``/api/v1``):
* ``GET    /memclaw/keystones`` — list scope-merged rules
* ``POST   /memclaw/keystones`` — upsert a rule (trust ≥ 2)
* ``DELETE /memclaw/keystones/{doc_id}`` — remove a rule (trust ≥ 2)

Surface the ``X-Truncated`` header from core-storage so callers can warn
operators when rules are being silently dropped.
"""

from __future__ import annotations

import logging
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import KeystoneUpsertPayload, get_storage_client
from core_api.db.session import get_db
from core_api.services.audit_service import log_action
from core_api.services.trust_service import parse_trust_error
from core_api.services.trust_service import require_trust as _require_trust

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/memclaw/keystones", tags=["Keystones"])


# ── Schemas ──


class KeystoneSetRequest(BaseModel):
    """Payload shape mirrors the storage-api validator one-for-one so we
    don't need to re-do the scope/weight/fleet shape checks here — the
    storage 422 propagates through."""

    tenant_id: str
    fleet_id: str | None = None
    agent_id: str | None = None
    # Slug shape mirrors ``memclaw_doc`` collection=skills (filesystem-safe
    # identifier) so keystone ``doc_id`` values stay greppable in audit
    # logs and safe to render in dashboards. The pattern already pins
    # length (1 leading char + up to 99 trailing), so explicit ``min_length``
    # / ``max_length`` would be redundant.
    doc_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$")
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    scope: Literal["tenant", "fleet", "agent"]
    weight: Literal["low", "med", "high"]
    author_user_id: str | None = None


# ── Helpers ──


async def _enforce_author_trust(
    db: AsyncSession,
    tenant_id: str,
    agent_id: str,
    *,
    min_level: int = 2,
) -> None:
    """Block keystone writes from low-trust principals.

    Trust ≥ 2 is the gate. Keystones override user instructions across
    the tenant, so a freshly-registered default-trust (=1) agent must
    not be able to plant one — the same elevated tier used elsewhere
    for cross-agent operations (``memclaw_list/stats/evolve/insights``
    with ``scope=fleet|all``).

    ``require_trust`` soft-passes when no agent row exists AND
    ``min_level <= DEFAULT_TRUST_LEVEL`` — that's wrong here. Identity
    attribution on a keystone has to be verifiable; an unregistered
    fabricated ``agent_id`` would corrupt the audit trail. So we check
    ``not_found`` independently (the documented write-path pattern,
    matches ``routes/evolve.py``).

    **Cross-fleet authoring is intentionally allowed at this layer.**
    A trust ≥ 2 agent in tenant T can write a ``scope=fleet`` or
    ``scope=agent`` rule for any fleet within T — there is no check
    that ``body.fleet_id`` matches the caller's pinned fleet. Rationale:

      * Governance / org-admin workflows legitimately need to set
        fleet-scope rules for fleets the caller doesn't run agents in.
      * Tenant isolation (RLS + ``tenant_id`` filters) already prevents
        cross-tenant writes — the remaining surface is intra-tenant.
      * A finer-grained scope-authority model (per-agent fleet pinning,
        admin/org-owner role, ``scope=tenant`` from fleet-pinned agents)
        needs deliberate design across several edge cases — out of scope
        for the keystone surface PR.

    Tracked as a follow-up: tighten so non-admin agents can only author
    keystones for their own fleet (or ``scope=tenant`` with an admin
    role). Until then, trust ≥ 2 is the single gate.
    """
    _trust, not_found, terr = await _require_trust(db, tenant_id, agent_id, min_level=min_level)
    if not_found:
        raise HTTPException(
            status_code=403,
            detail=(f"Agent '{agent_id}' is not registered. Register the agent by writing one memory first."),
        )
    if terr:
        raise HTTPException(status_code=403, detail=parse_trust_error(terr))


def _author_agent_id(auth: AuthContext, x_agent_id: str | None) -> str:
    """Author identity for trust + audit.

    Precedence:
      1. ``auth.agent_id`` — gateway-verified, only set when the caller
         used an agent-scoped key (e.g. ``mca_…``).
      2. ``X-Agent-ID`` header — used by admin/governance tooling acting
         on behalf of a specific agent. The admin-key auth path drops
         this from ``AuthContext`` (admin keys aren't pinned to an
         agent), so we read it separately here.
      3. ``"rest-admin"`` fallback — unattributed admin call. The trust
         check then 403s (no agent row), which is the correct behaviour:
         keystone writes must be traceable to a registered identity.
    """
    return getattr(auth, "agent_id", None) or x_agent_id or "rest-admin"


def _surface_storage_error(exc: httpx.HTTPStatusError) -> HTTPException:
    """Translate a storage-api ``HTTPStatusError`` into an ``HTTPException``
    so the caller sees the original status (e.g. storage's 422 validator
    output) instead of a 500. ``storage_client._post`` raises on non-2xx,
    so writes that fail storage-side shape validation bubble up here."""
    detail: object
    try:
        detail = exc.response.json()
    except ValueError:
        detail = exc.response.text or str(exc)
    return HTTPException(status_code=exc.response.status_code, detail=detail)


# ── Routes ──


@router.get("")
async def list_keystones(
    response: Response,
    tenant_id: str = Query(...),
    fleet_id: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
):
    """Return scope-merged keystone rules. No trust gate — reads are
    safe and the plugin needs this on every session start."""
    auth.enforce_tenant(tenant_id)
    sc = get_storage_client()
    # Drop ``agent_id`` when there's no ``fleet_id`` — agent-scope rows
    # are keyed on the (fleet_id, agent_id) pair, so an agent-only filter
    # can't resolve them. Mirrors the MCP handler's guard so both
    # surfaces return identical results for the same input.
    try:
        rows, truncated = await sc.list_keystones(
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            agent_id=agent_id if fleet_id else None,
        )
    except httpx.HTTPStatusError as exc:
        raise _surface_storage_error(exc) from exc
    if truncated:
        response.headers["X-Truncated"] = "true"
    return rows


@router.post("")
async def upsert_keystone(
    body: KeystoneSetRequest,
    x_agent_id: str | None = Header(default=None, alias="X-Agent-ID"),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Upsert a keystone rule. Requires trust ≥ 2."""
    auth.enforce_tenant(body.tenant_id)
    # ``enforce_read_only`` gates demo sandboxes; ``enforce_usage_limits``
    # gates plan-exceeded orgs. Write routes must call both — delete
    # routes only the former (see usage_service docstring).
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    caller_agent_id = _author_agent_id(auth, x_agent_id)
    await _enforce_author_trust(db, body.tenant_id, caller_agent_id)

    sc = get_storage_client()
    # Pass-through to storage — it owns scope/weight/agent_id shape
    # validation; surface its 422 directly so the caller sees a single
    # canonical error list.
    # Build the TypedDict explicitly so mypy catches missing required
    # fields here, not at the network boundary. Storage treats a present
    # ``"fleet_id": None`` differently from an absent key (scope=tenant
    # must not include fleet_id), so optional fields are added only when
    # set rather than included as None.
    payload: KeystoneUpsertPayload = {
        "tenant_id": body.tenant_id,
        "doc_id": body.doc_id,
        "title": body.title,
        "content": body.content,
        "scope": body.scope,
        "weight": body.weight,
    }
    if body.fleet_id is not None:
        payload["fleet_id"] = body.fleet_id
    if body.agent_id is not None:
        payload["agent_id"] = body.agent_id
    if body.author_user_id is not None:
        payload["author_user_id"] = body.author_user_id

    try:
        doc = await sc.upsert_keystone(payload)
    except httpx.HTTPStatusError as exc:
        raise _surface_storage_error(exc) from exc

    await log_action(
        db,
        tenant_id=body.tenant_id,
        agent_id=caller_agent_id,
        action="keystone.set",
        resource_type="keystone",
        resource_id=doc.get("id"),
        detail={
            "doc_id": body.doc_id,
            "scope": body.scope,
            "fleet_id": body.fleet_id,
            "agent_id": body.agent_id,
            "weight": body.weight,
            "author_user_id": body.author_user_id,
            "via": "rest",
        },
    )
    await db.commit()
    return doc


@router.delete("/{doc_id}")
async def delete_keystone(
    # Enforce the slug shape at the path-parameter layer — without this
    # an unvalidated ``doc_id`` flows straight into ``storage_client``'s
    # f-string URL construction, where ``..`` would resolve to the
    # storage parent path. Matches ``KeystoneSetRequest.doc_id``'s
    # Pydantic ``pattern``.
    doc_id: str = Path(..., pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$"),
    tenant_id: str = Query(...),
    x_agent_id: str | None = Header(default=None, alias="X-Agent-ID"),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Remove a keystone rule. Requires trust ≥ 2."""
    auth.enforce_tenant(tenant_id)
    auth.enforce_read_only()
    await _enforce_author_trust(db, tenant_id, _author_agent_id(auth, x_agent_id))

    sc = get_storage_client()
    try:
        deleted = await sc.delete_keystone(tenant_id=tenant_id, doc_id=doc_id)
    except httpx.HTTPStatusError as exc:
        raise _surface_storage_error(exc) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Keystone not found")

    await log_action(
        db,
        tenant_id=tenant_id,
        agent_id=_author_agent_id(auth, x_agent_id),
        action="keystone.delete",
        resource_type="keystone",
        resource_id=None,
        detail={"doc_id": doc_id, "via": "rest"},
    )
    await db.commit()
    return {"deleted": True, "doc_id": doc_id}
