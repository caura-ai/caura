"""Keystone-rules CRUD endpoints.

Thin wrapper over the documents store (collection ``_keystones``).
Trust enforcement lives upstream in core-api; storage assumes its
caller has already authorised the principal.

Endpoints (all under the storage prefix ``/api/v1/storage``):

* ``GET    /keystones`` — list resolved scope union
* ``POST   /keystones`` — upsert a rule
* ``DELETE /keystones/{doc_id}`` — remove a rule
* ``GET    /keystones/versions`` — the tenant's versions, newest first
* ``GET    /keystones/versions/{version}`` — one version and its rules

Every write records a version (migration 061) and emits an audit row
(``CAURA-000``). The writes take who made them as ``actor_agent_id`` and
``actor_user_id``, the body's for a set and the query's for a delete.
"""

from __future__ import annotations

import logging
import re
from typing import cast

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response

from core_storage_api.schemas import DOCUMENT_FIELDS, orm_to_dict
from core_storage_api.services.keystones import (
    KEYSTONE_VALID_SCOPES,
    KEYSTONE_WEIGHT_BUCKETS,
    get_keystone_version,
    list_keystone_versions,
    list_keystones,
    remove_keystone,
    set_keystone,
)
from core_storage_api.services.postgres_service import PostgresService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/keystones", tags=["Keystones"])
_svc = PostgresService()

# Filesystem-safe slug. Mirrors the edge validators (core-api
# KeystoneSetRequest.doc_id Field(pattern=...) and mcp_server's keystones_set
# regex) so the storage validator enforces the same contract its callers and
# docs promise, even for direct storage clients.
_DOC_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")

# Text Postgres can't read back: a NUL, and an unpaired surrogate, which is no
# character at all. ``documents.data`` is ``json`` and stores either as its
# escape, but ``->>`` then fails on the whole document. The list reads
# ``data->>'scope'`` on every keystone in the tenant, so one such rule would
# fail every agent's list. (A ``text`` column, such as ``fleet_id``'s, refuses
# both outright.)
_UNREADABLE_TEXT_RE = re.compile(r"[\x00\ud800-\udfff]")

# ``keystone_versions.version`` is an int4. A larger number can't be bound to
# a query, so the versions routes refuse it rather than fail with a 500.
_MAX_VERSION = 2**31 - 1


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_payload(body: dict) -> tuple[str, dict, str | None]:
    """Validate a POST body and return ``(doc_id, normalised_data, fleet_id)``.

    Rejects missing fields, unknown scope, weight bucket mismatch,
    agent_id mismatch with scope, and text Postgres can't read back. We
    reject (not coerce) on missing scope per the spec — silent defaults
    hide bugs.
    """
    # Every string in the body, so a field added later is covered too.
    errors: list[str] = [
        f"{key} must not contain a NUL character or an unpaired surrogate"
        for key, value in body.items()
        if isinstance(value, str) and _UNREADABLE_TEXT_RE.search(value)
    ]

    doc_id = body.get("doc_id")
    if not doc_id or not isinstance(doc_id, str):
        errors.append("doc_id is required and must be a string")
    elif not _DOC_ID_RE.match(doc_id):
        errors.append(
            f"doc_id must match {_DOC_ID_RE.pattern} — "
            "a stable kebab-case slug you choose (e.g. 'redis-pool-size-bump'); "
            "re-using it upserts the rule"
        )

    title = body.get("title")
    if not title or not isinstance(title, str):
        errors.append("title is required and must be a string")

    content = body.get("content")
    if not content or not isinstance(content, str):
        errors.append("content is required and must be a string")

    weight_label = body.get("weight")
    if not weight_label:
        errors.append(f"weight is required and must be one of {sorted(KEYSTONE_WEIGHT_BUCKETS)}")
    elif weight_label not in KEYSTONE_WEIGHT_BUCKETS:
        errors.append(f"weight must be one of {sorted(KEYSTONE_WEIGHT_BUCKETS)}; got {weight_label!r}")

    scope = body.get("scope")
    if scope not in KEYSTONE_VALID_SCOPES:
        errors.append(f"scope must be one of {sorted(KEYSTONE_VALID_SCOPES)}; got {scope!r}")

    fleet_id = body.get("fleet_id")
    agent_id = body.get("agent_id")

    # Type guards — JSON bodies are untyped, and non-string identifiers
    # would slip past the scope-shape checks below (``not fleet_id`` is
    # True for both ``None`` and ``0``) and produce malformed rows.
    if fleet_id is not None and not isinstance(fleet_id, str):
        errors.append("fleet_id must be a string")
    if agent_id is not None and not isinstance(agent_id, str):
        errors.append("agent_id must be a string")

    # Optional names: the author the body claims, and who made the change, for
    # its version (core-api sends the calling agent and the person the gateway
    # vouched for when it knows them).
    for key in ("author_user_id", "actor_agent_id", "actor_user_id"):
        value = body.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            errors.append(f"{key} must be a non-empty string")
    author_user_id = body.get("author_user_id")

    # Scope-specific shape rules. We only enforce these when scope itself
    # validated above — otherwise the error list double-reports.
    if scope in KEYSTONE_VALID_SCOPES:
        if scope == "tenant" and fleet_id is not None:
            errors.append("scope=tenant must not include fleet_id")
        if scope in {"fleet", "agent"} and not fleet_id:
            errors.append(f"scope={scope} requires fleet_id")
        if scope == "agent" and not agent_id:
            errors.append("scope=agent requires agent_id")
        if scope != "agent" and agent_id is not None:
            errors.append(
                f"scope={scope} keystones apply {scope}-wide and are not agent-specific; "
                "omit agent_id (it is only valid for scope=agent)"
            )

    if errors:
        raise HTTPException(status_code=422, detail=errors)

    # Validation above guarantees these are well-typed strings; ``cast``
    # is a static-only hint (no runtime cost) so mypy doesn't trip on
    # the ``Any | None`` returned by ``body.get(...)``.
    doc_id_s = cast("str", doc_id)
    weight_label_s = cast("str", weight_label)

    # No timestamp fields in `data` — the Document ORM exposes
    # ``created_at`` and ``updated_at`` columns via DOCUMENT_FIELDS, and
    # an in-payload timestamp would be overwritten on every upsert.
    data: dict = {
        "title": title,
        "content": content,
        "weight": KEYSTONE_WEIGHT_BUCKETS[weight_label_s],
        "scope": scope,
    }
    if scope == "agent":
        data["agent_id"] = agent_id
    # author_user_id is informational — pass through if supplied.
    if author_user_id:
        data["author_user_id"] = author_user_id

    return doc_id_s, data, fleet_id


def _require_scope(tenant_id: str, fleet_id: str | None, agent_id: str | None) -> None:
    """The read routes' shared checks on whose rules are asked for."""
    # ``agent_id`` without ``fleet_id`` cannot resolve to any agent-scope
    # row (fleet_id is part of the agent-scope key) — accepting the call
    # would silently degrade to fleet/tenant scope and hide a caller bug.
    if agent_id is not None and fleet_id is None:
        raise HTTPException(status_code=422, detail=["agent_id requires fleet_id"])

    if not tenant_id:
        raise HTTPException(status_code=422, detail=["tenant_id is required"])


async def _audit(
    *,
    tenant_id: str,
    action: str,
    resource_id,  # UUID | None
    detail: dict,
) -> None:
    """Best-effort audit emission.

    ``audit_add`` and the keystone write (with its version) use
    independent DB sessions; Postgres can't roll one back from the
    other without explicit 2PC. So if audit fails AFTER the document
    write committed, surfacing the audit error as 500 would tell the
    client "your write failed" while the rule sits persisted — strictly
    worse than a missing audit row. We log loudly instead so the gap is
    investigable, and the operation reports its actual outcome.
    """
    try:
        await _svc.audit_add(
            tenant_id=tenant_id,
            agent_id=None,
            action=action,
            resource_type="keystone",
            resource_id=resource_id,
            detail=detail,
        )
    except Exception:
        logger.exception(
            "keystone audit_add failed; document state persisted",
            extra={
                "tenant_id": tenant_id,
                "action": action,
                "resource_id": str(resource_id) if resource_id else None,
                "detail": detail,
            },
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def get_keystones(
    response: Response,
    tenant_id: str,
    fleet_id: str | None = None,
    agent_id: str | None = None,
) -> list[dict]:
    """Return scope-merged keystones for the requested principal.

    Sets ``X-Truncated: true`` if the result was capped at
    ``KEYSTONE_MAX_RESULTS`` so callers can warn an operator that
    rules are silently being dropped from the agent's context.
    """
    _require_scope(tenant_id, fleet_id, agent_id)

    docs, truncated = await list_keystones(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
    )
    if truncated:
        response.headers["X-Truncated"] = "true"
    return [orm_to_dict(d, DOCUMENT_FIELDS) for d in docs]


@router.post("")
async def upsert_keystone(request: Request) -> dict:
    body: dict = await request.json()

    tenant_id = body.get("tenant_id")
    if not tenant_id or not isinstance(tenant_id, str):
        raise HTTPException(status_code=422, detail=["tenant_id is required and must be a string"])

    doc_id, data, fleet_id = _validate_payload(body)

    try:
        doc, version = await set_keystone(
            tenant_id=tenant_id,
            doc_id=doc_id,
            data=data,
            fleet_id=fleet_id,
            actor_agent_id=body.get("actor_agent_id"),
            actor_user_id=body.get("actor_user_id"),
        )
    except ValueError as exc:
        # The document upsert's refusals, a 400 as on ``POST /documents``,
        # not a 500 (L-39).
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _audit(
        tenant_id=tenant_id,
        action="keystone.set",
        resource_id=doc.id,
        detail={
            "doc_id": doc_id,
            "scope": data["scope"],
            "fleet_id": fleet_id,
            "agent_id": data.get("agent_id"),
            "weight": data["weight"],
            "author_user_id": data.get("author_user_id"),
            "version": version,
        },
    )
    return orm_to_dict(doc, DOCUMENT_FIELDS)


@router.delete("/{doc_id}")
async def delete_keystone(
    doc_id: str,
    tenant_id: str,
    actor_agent_id: str | None = Query(default=None, min_length=1),
    actor_user_id: str | None = Query(default=None, min_length=1),
) -> dict:
    if not tenant_id:
        raise HTTPException(status_code=422, detail=["tenant_id is required"])
    removed = await remove_keystone(
        tenant_id=tenant_id,
        doc_id=doc_id,
        actor_agent_id=actor_agent_id,
        actor_user_id=actor_user_id,
    )
    if removed is None:
        raise HTTPException(status_code=404, detail="Keystone not found")
    deleted_id, version = removed
    await _audit(
        tenant_id=tenant_id,
        action="keystone.delete",
        resource_id=deleted_id,
        detail={"doc_id": doc_id, "version": version},
    )
    return {"deleted_id": str(deleted_id)}


@router.get("/versions")
async def keystone_versions(
    tenant_id: str,
    fleet_id: str | None = None,
    agent_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    before: int | None = Query(default=None, ge=1, le=_MAX_VERSION),
) -> dict:
    """The tenant's keystone versions, newest first, each with the rule-set
    hash of what it gives ``(fleet_id, agent_id)``.

    ``next_before`` pages on: pass it back as ``before``. ``None`` on the
    last page.
    """
    _require_scope(tenant_id, fleet_id, agent_id)
    items, next_before = await list_keystone_versions(
        tenant_id=tenant_id,
        fleet_id=fleet_id,
        agent_id=agent_id,
        limit=limit,
        before=before,
    )
    return {"count": len(items), "items": items, "next_before": next_before}


@router.get("/versions/{version}")
async def keystone_version(
    tenant_id: str,
    version: int = Path(..., ge=1, le=_MAX_VERSION),
    fleet_id: str | None = None,
    agent_id: str | None = None,
) -> dict:
    """One version, with the rules it gives ``(fleet_id, agent_id)`` in the
    list's order as ``items``."""
    _require_scope(tenant_id, fleet_id, agent_id)
    found = await get_keystone_version(
        tenant_id=tenant_id,
        version=version,
        fleet_id=fleet_id,
        agent_id=agent_id,
    )
    if found is None:
        raise HTTPException(status_code=404, detail="Keystone version not found")
    return found
