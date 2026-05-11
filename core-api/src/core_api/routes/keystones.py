"""Keystone rules — REST surface (CAURA-000).

Public mirror of the ``memclaw_keystones`` / ``memclaw_keystones_set``
MCP tools. Thin proxy over core-storage's ``/api/v1/storage/keystones``;
trust enforcement (≥1 to author) and audit live in core-api so the
storage layer can stay a dumb CRUD service.

Endpoints (under ``/api/v1``):
* ``GET    /memclaw/keystones`` — list scope-merged rules
* ``POST   /memclaw/keystones`` — upsert a rule (trust ≥ 1)
* ``DELETE /memclaw/keystones/{doc_id}`` — remove a rule (trust ≥ 1)

Surface the ``X-Truncated`` header from core-storage so callers can warn
operators when rules are being silently dropped.
"""

from __future__ import annotations

import logging
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
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
    # logs and safe to render in dashboards.
    doc_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9._-]{0,99}$")
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
    min_level: int = 1,
) -> None:
    """Block keystone writes from low-trust principals.

    Trust ≥ 1 is the gate: keystones override user instructions, so a
    prompt-injection-driven write would otherwise plant a malicious rule
    that auto-injects into every future session.

    ``require_trust`` soft-passes when no agent row exists AND
    ``min_level <= DEFAULT_TRUST_LEVEL`` — that's wrong here. Identity
    attribution on a keystone has to be verifiable; an unregistered
    fabricated ``agent_id`` would corrupt the audit trail. So we check
    ``not_found`` independently (the documented write-path pattern,
    matches ``routes/evolve.py``).
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
    rows, truncated = await sc.list_keystones(tenant_id=tenant_id, fleet_id=fleet_id, agent_id=agent_id)
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
    """Upsert a keystone rule. Requires trust ≥ 1."""
    auth.enforce_tenant(body.tenant_id)
    auth.enforce_read_only()
    caller_agent_id = _author_agent_id(auth, x_agent_id)
    await _enforce_author_trust(db, body.tenant_id, caller_agent_id)

    sc = get_storage_client()
    # Pass-through to storage — it owns scope/weight/agent_id shape
    # validation; surface its 422 directly so the caller sees a single
    # canonical error list.
    try:
        doc = await sc.upsert_keystone(body.model_dump(exclude_none=False))
    except httpx.HTTPStatusError as exc:
        raise _surface_storage_error(exc) from exc

    await log_action(
        db,
        tenant_id=body.tenant_id,
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
    doc_id: str,
    tenant_id: str = Query(...),
    x_agent_id: str | None = Header(default=None, alias="X-Agent-ID"),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Remove a keystone rule. Requires trust ≥ 1."""
    auth.enforce_tenant(tenant_id)
    auth.enforce_read_only()
    await _enforce_author_trust(db, tenant_id, _author_agent_id(auth, x_agent_id))

    sc = get_storage_client()
    deleted = await sc.delete_keystone(tenant_id=tenant_id, doc_id=doc_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Keystone not found")

    await log_action(
        db,
        tenant_id=tenant_id,
        action="keystone.delete",
        resource_type="keystone",
        resource_id=None,
        detail={"doc_id": doc_id, "via": "rest"},
    )
    await db.commit()
    return {"deleted": True, "doc_id": doc_id}
