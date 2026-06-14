"""Keystone rules — REST surface (CAURA-000).

Public mirror of the ``memclaw_keystones`` / ``memclaw_keystones_set``
MCP tools. Thin proxy over core-storage's ``/api/v1/storage/keystones``;
tiered trust enforcement (see the matrix below) and audit live in
core-api so the storage layer can stay a dumb CRUD service.

Endpoints (under ``/api/v1``):
* ``GET    /memclaw/keystones`` — list scope-merged rules
* ``POST   /memclaw/keystones`` — upsert a rule (tiered trust; see below)
* ``DELETE /memclaw/keystones/{doc_id}`` — remove a rule (tiered trust)

Trust gating is dynamic per the targeted rule's scope:

* ``scope=agent`` AND ``agent_id == caller`` → **trust ≥ 1** (self-author).
* Anything else (``scope=fleet`` / ``scope=tenant`` / cross-agent
  ``scope=agent``) → **trust ≥ 2**.

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
from core_api.config import settings as app_settings
from core_api.db.session import get_db
from core_api.services.audit_service import log_action
from core_api.services.trust_service import parse_trust_error
from core_api.services.trust_service import require_trust as _require_trust
from core_api.trust_utils import effective_keystone_min_trust, keystone_min_trust

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
    min_level: int,
    standalone_admin: bool = False,
) -> None:
    """Block keystone writes / deletes from principals below ``min_level``.

    Callers compute ``min_level`` via
    :func:`core_api.trust_utils.keystone_min_trust` (or
    :func:`~core_api.trust_utils.effective_keystone_min_trust` for upserts
    against an existing rule). The check itself
    is the standard write-path pattern (mirrors ``routes/evolve.py``):
    ``require_trust`` soft-passes when no agent row exists AND
    ``min_level <= DEFAULT_TRUST_LEVEL``, so the ``not_found`` branch
    is rejected explicitly — keystone writes must be traceable to a
    registered identity, and the soft-pass would let a fabricated
    ``agent_id`` through.

    **Cross-fleet authoring at trust ≥ 2 is intentionally allowed.**
    A trust-2 agent in tenant T can still write ``scope=fleet`` rules
    for any fleet within T — finer-grained scope authority (admin/org
    role, fleet pinning) is tracked separately (#119).
    """
    # Standalone single-tenant operator: the API-key holder IS the admin and
    # there is no other agent to impersonate, so the anti-spoof trust gate is
    # pure friction. Skip it (the caller still passes storage-side shape
    # validation). See ``_is_standalone_admin``.
    if standalone_admin:
        return
    _trust, not_found, terr = await _require_trust(db, tenant_id, agent_id, min_level=min_level)
    if not_found:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Agent '{agent_id}' has no registered agent row, so its keystone-author "
                "trust can't be verified. Register it (write one memory as that agent, then "
                "promote its trust), or call with X-Agent-ID / an agent-scoped credential for "
                "an agent at trust ≥ 2."
            ),
        )
    if terr:
        raise HTTPException(status_code=403, detail=parse_trust_error(terr))


def _resolve_caller_identity(auth: AuthContext, x_agent_id: str | None) -> tuple[str, bool]:
    """Return ``(caller_agent_id, verified)`` for the request.

    ``verified=True`` means the gateway cryptographically established
    the caller's agent identity (an agent-scoped credential whose
    ``kind=agent_key`` populated ``auth.agent_id``). ``verified=False``
    means the identity
    is asserted via the ``X-Agent-ID`` header alone — which is what
    happens when a non-agent-scoped (admin / tenant) key is in use.
    Unverified identities are still accepted but with stricter trust
    gating downstream — see ``_effective_min_for_caller``.

    Mismatch rejection: when both signals are present and disagree,
    the caller is treated as a spoofing attempt and rejected outright
    rather than letting the helper silently pick one. Pre-fix, an
    attacker holding an admin key could supply ``X-Agent-ID`` for any
    trust-1 victim and forge keystones in the victim's name; mismatch
    handling is one half of the defence, the other is the floor bump
    in ``_effective_min_for_caller``.

    Fallback ``"rest-admin"`` is preserved for legacy callers — the
    trust check 403s on it anyway (no agent row exists), but the
    fallback keeps the error surface predictable.
    """
    verified_id = getattr(auth, "agent_id", None)
    if verified_id and x_agent_id and verified_id != x_agent_id:
        raise HTTPException(
            status_code=403,
            detail=(
                "X-Agent-ID header does not match authenticated identity. "
                "Refusing to act on behalf of a different agent."
            ),
        )
    if verified_id:
        return verified_id, True
    if x_agent_id:
        return x_agent_id, False
    return "rest-admin", False


def _is_standalone_admin(auth: AuthContext, x_agent_id: str | None) -> bool:
    """True for the unidentified single-tenant operator on a standalone box.

    Under ``IS_STANDALONE`` there is exactly one principal — the API-key
    holder — and no other agent to impersonate, so the keystone trust gate
    that protects multi-tenant / gateway deployments is pure friction here
    (the flagship governance feature otherwise 403s on a fresh install).

    Deliberately narrow: fires ONLY when the caller asserts no agent identity
    at all — no agent-scoped credential (``auth.agent_id``) and no
    ``X-Agent-ID`` header — i.e. the ``rest-admin`` fallback in
    ``_resolve_caller_identity``. A request that names an agent via
    ``X-Agent-ID`` still goes through the normal trust gate, so the anti-spoof
    defenses (floor bump, mismatch rejection) are untouched, and this is a
    no-op whenever ``IS_STANDALONE`` is false.
    """
    return app_settings.is_standalone and getattr(auth, "agent_id", None) is None and x_agent_id is None


def _effective_min_for_caller(scope_floor: int, caller_verified: bool) -> int:
    """Bump the trust floor when the caller's identity is unverified.

    The self-author tier (``scope=agent`` for one's own ``agent_id``)
    is open at trust ≥ 1 precisely because we KNOW the caller IS the
    target. If we don't know — i.e. the caller is only asserting their
    identity via the unverified ``X-Agent-ID`` header — fall back to
    the cross-agent governance bar (≥ 2). Otherwise an admin-key
    holder could spoof any registered trust-1 agent and plant a rule
    in that agent's name.
    """
    if not caller_verified and scope_floor < 2:
        return 2
    return scope_floor


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
    safe and the plugin needs this on every session start.

    Cross-tenant credentials may inspect any tenant in their readable set
    by pinning ``tenant_id`` to it; the read is scoped to that single
    tenant's rules. Aggregate keystone view across the readable set
    isn't exposed here — agents should keep keystones explicitly per
    tenant for scope clarity.
    """
    auth.enforce_readable_tenant(tenant_id)
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
    """Upsert a keystone rule. Trust ≥ 1 for self-authored ``scope=agent``
    rules; ≥ 2 otherwise. See module docstring."""
    auth.enforce_tenant(body.tenant_id)
    # ``enforce_read_only`` gates demo sandboxes; ``enforce_usage_limits``
    # gates plan-exceeded orgs. Write routes must call both — delete
    # routes only the former (see usage_service docstring).
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    caller_agent_id, caller_verified = _resolve_caller_identity(auth, x_agent_id)
    standalone_admin = _is_standalone_admin(auth, x_agent_id)

    # Early registration check — anti-probing parity with delete. Without
    # this, an unregistered caller could probe ``doc_id`` existence
    # because ``sc.get_document`` below runs before any trust check
    # fires. Use the minimum floor (1) here so a trust-1 caller passes;
    # the full floor (which may be 2 once the stored shape is known) is
    # re-enforced after the storage read.
    await _enforce_author_trust(
        db, body.tenant_id, caller_agent_id, min_level=1, standalone_admin=standalone_admin
    )

    sc = get_storage_client()
    # Look up the existing rule (if any) so the trust floor combines
    # the NEW body shape and the STORED shape. Without this, a trust-1
    # agent could overwrite a ``scope=fleet`` rule by submitting
    # ``scope=agent`` + ``agent_id=<self>`` — the new-shape floor (1)
    # would pass the gate and storage would upsert unconditionally,
    # silently replacing a tenant-wide rule with one only the attacker
    # controls. ``effective_keystone_min_trust`` returns the max of the
    # two floors so the caller must be authorised for whichever shape
    # is stricter.
    existing = await sc.get_document(tenant_id=body.tenant_id, collection="_keystones", doc_id=body.doc_id)
    existing_data = (existing or {}).get("data") or {}
    scope_floor = effective_keystone_min_trust(
        new_scope=body.scope,
        new_target_agent_id=body.agent_id,
        stored_scope=existing_data.get("scope") if existing else None,
        stored_target_agent_id=existing_data.get("agent_id") if existing else None,
        caller_agent_id=caller_agent_id,
    )
    # Bump the floor when the caller's identity is unverified — self-
    # author tier requires we KNOW who the caller is. Without this, an
    # admin-key holder could supply ``X-Agent-ID=<victim>`` plus
    # ``scope=agent``+``agent_id=<victim>`` and forge a rule in the
    # victim's name at trust 1.
    min_level = _effective_min_for_caller(scope_floor, caller_verified)
    await _enforce_author_trust(
        db, body.tenant_id, caller_agent_id, min_level=min_level, standalone_admin=standalone_admin
    )
    # TOCTOU narrowing: re-fetch the stored row immediately before the
    # upsert and abort with 409 if the shape changed. A legitimate
    # concurrent upsert could otherwise promote the stored scope
    # between the gate read and the write below, letting a caller
    # authorised for the looser earlier shape overwrite a stricter
    # rule. Window is now reduced to (recheck → write), matching the
    # delete path; storage-side conditional upsert (e.g. WHERE scope=?
    # AND agent_id IS NOT DISTINCT FROM ?) remains the proper fix.
    recheck = await sc.get_document(tenant_id=body.tenant_id, collection="_keystones", doc_id=body.doc_id)
    recheck_data = (recheck or {}).get("data") or {}
    if (existing is None) != (recheck is None) or (
        existing is not None
        and recheck is not None
        and (
            recheck_data.get("scope") != existing_data.get("scope")
            or recheck_data.get("agent_id") != existing_data.get("agent_id")
        )
    ):
        raise HTTPException(
            status_code=409,
            detail="Keystone scope changed during operation; aborting upsert.",
        )
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
    """Remove a keystone rule. Trust ≥ 1 to delete a self-authored
    ``scope=agent`` rule; ≥ 2 otherwise. The rule is fetched first so
    the gate can read the actual scope/agent_id from the stored row
    rather than trusting any caller assertion."""
    auth.enforce_tenant(tenant_id)
    auth.enforce_read_only()
    caller_agent_id, caller_verified = _resolve_caller_identity(auth, x_agent_id)
    standalone_admin = _is_standalone_admin(auth, x_agent_id)
    trust: int = 0  # assigned in the trust-gate block below; default unused (read is in the same not-standalone_admin guard)

    # ONE trust round-trip for both the pre-lookup registration check
    # (≥ 1, anti-probing) and the post-lookup floor check. We ask
    # ``_require_trust`` for the minimum the caller could possibly
    # need (1), then compare the returned trust level against the
    # floor computed from the stored rule. This collapses two DB
    # queries into one without losing either guarantee. Skipped for the
    # standalone single-tenant operator (see ``_is_standalone_admin``).
    if not standalone_admin:
        trust, not_found, terr = await _require_trust(db, tenant_id, caller_agent_id, min_level=1)
        # Anti-probing: an unregistered caller must NOT learn whether a
        # ``doc_id`` exists (404 would leak presence; trust check below
        # would 403). 403 unconditionally on missing identity.
        if not_found:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Agent '{caller_agent_id}' has no registered agent row, so its "
                    "keystone-author trust can't be verified. Register it (write one memory "
                    "as that agent, then promote its trust), or call with X-Agent-ID / an "
                    "agent-scoped credential for an agent at trust ≥ 2."
                ),
            )
        if terr:
            raise HTTPException(status_code=403, detail=parse_trust_error(terr))

    sc = get_storage_client()
    # Look up the rule before computing the scope-derived floor — the
    # documents-store GET ignores the system-collection guard (which
    # only fires on write/delete), so this needs no new endpoint.
    existing = await sc.get_document(tenant_id=tenant_id, collection="_keystones", doc_id=doc_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Keystone not found")
    data = existing.get("data") or {}
    if not standalone_admin:
        scope_floor = keystone_min_trust(
            data.get("scope", ""),
            data.get("agent_id"),
            caller_agent_id,
        )
        # Bump to ≥ 2 if the caller's identity is unverified (admin key
        # with ``X-Agent-ID`` claim only) — same anti-spoof rationale as
        # the upsert path.
        min_level = _effective_min_for_caller(scope_floor, caller_verified)
        if trust < min_level:
            raise HTTPException(
                status_code=403,
                detail=(f"Agent '{caller_agent_id}' (trust_level={trust}) < required {min_level}."),
            )

    # TOCTOU narrowing: re-fetch the stored row immediately before the
    # delete and abort with 409 if the shape changed. Without this, a
    # legitimate concurrent upsert can promote a ``scope=agent`` rule
    # to ``scope=fleet`` between the first read and the storage delete,
    # letting a trust-1 caller delete a now-fleet rule it was never
    # authorised for — both flows pass their own gates individually
    # but the net effect bypasses the cross-agent governance bar. The
    # race window is now reduced to (recheck → delete), which is a
    # storage round-trip only; the proper fix is a storage-side
    # compare-and-delete with scope/agent_id preconditions, tracked
    # for a follow-up.
    recheck = await sc.get_document(tenant_id=tenant_id, collection="_keystones", doc_id=doc_id)
    if not recheck:
        raise HTTPException(status_code=404, detail="Keystone not found")
    recheck_data = recheck.get("data") or {}
    if recheck_data.get("scope") != data.get("scope") or recheck_data.get("agent_id") != data.get("agent_id"):
        raise HTTPException(
            status_code=409,
            detail="Keystone scope changed during operation; aborting delete.",
        )
    try:
        deleted = await sc.delete_keystone(tenant_id=tenant_id, doc_id=doc_id)
    except httpx.HTTPStatusError as exc:
        raise _surface_storage_error(exc) from exc
    if not deleted:
        # The row vanished between the lookup and the delete (concurrent
        # delete from another caller). Surface as 404, same as the
        # original missing-row case.
        raise HTTPException(status_code=404, detail="Keystone not found")

    await log_action(
        db,
        tenant_id=tenant_id,
        agent_id=caller_agent_id,
        action="keystone.delete",
        resource_type="keystone",
        resource_id=None,
        detail={"doc_id": doc_id, "via": "rest"},
    )
    await db.commit()
    return {"deleted": True, "doc_id": doc_id}
