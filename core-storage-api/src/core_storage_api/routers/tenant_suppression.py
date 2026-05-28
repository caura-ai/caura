"""Tenant suppression mirror (CAURA-694).

Stores one row per tenant the enterprise org-lifecycle has ever
touched. Two endpoints:

  - ``POST /tenant-suppression`` — upsert for the OSS suppression
    consumer (core-worker in SaaS, core-api in OSS-standalone if it
    grows a subscriber). Body: ``{tenant_id, action, updated_by?}``
    where ``action`` is ``suppress`` | ``restore``.
  - ``GET  /tenant-suppression/{tenant_id}`` — boundary-guard read.
    Returns ``{tenant_id, suppressed_at, updated_at, updated_by}`` or
    a small ``{tenant_id, suppressed_at: null}`` for an unknown
    tenant. Hot path — kept light so core-api can call it on every
    authenticated request behind a small in-process cache.

Same trust model as the rest of core-storage-api (see
``routers/purge.py``): no router-level authentication; the deployment
runs storage on a VPC-internal endpoint and trusts upstream auth at
core-api. Do NOT expose this surface to the public internet.
"""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/tenant-suppression", tags=["TenantSuppression"])
_svc = PostgresService()


# Pydantic would be overkill for a two-field body; we validate inline
# to stay consistent with ``routers/purge.py``.
_ALLOWED_ACTIONS: set[str] = {"suppress", "restore"}


@router.post("")
async def upsert_tenant_suppression(request: Request) -> dict:
    """Upsert one row. Body: ``{tenant_id, action, updated_by?}``.

    Returns the resulting row so the caller can log the post-state
    without an extra GET. The service-layer ``set_tenant_suppression``
    is the single SQL source of truth for the ``suppress`` /
    ``restore`` semantics — this router only validates the wire
    contract.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # ``UnicodeDecodeError`` fires when the request body contains
        # invalid UTF-8 bytes — distinct from ``JSONDecodeError`` and
        # NOT a subclass of it, so a single-class catch missed it and
        # surfaced as 500. Bot review round 1 on PR #244 (🟢 Low).
        raise HTTPException(status_code=422, detail="request body must be valid JSON") from exc
    # A valid JSON body can also be an array / number / string — calling
    # ``.get`` on those raises ``AttributeError`` and 500s. Bot review
    # round 1 on PR #244 (🟡 Med).
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="request body must be a JSON object")
    tenant_id = body.get("tenant_id")
    action = body.get("action")
    updated_by = body.get("updated_by")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise HTTPException(
            status_code=422,
            detail="'tenant_id' is required and must be a non-empty string",
        )
    if action not in _ALLOWED_ACTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"'action' must be one of {sorted(_ALLOWED_ACTIONS)!r}",
        )
    # ``updated_by`` is optional; reject non-string values for the same
    # reason ``tenant_id`` rejects them — silent coercion of an int /
    # dict to ``str()`` masks an upstream bug.
    if updated_by is not None and not isinstance(updated_by, str):
        raise HTTPException(
            status_code=422,
            detail="'updated_by' must be a string when provided",
        )
    typed_action: Literal["suppress", "restore"] = action  # narrowed above
    row = await _svc.set_tenant_suppression(tenant_id, action=typed_action, updated_by=updated_by)
    return row


@router.get("/{tenant_id}")
async def get_tenant_suppression(tenant_id: str) -> dict:
    """Boundary-guard read. Returns ``{tenant_id, is_suppressed}``.

    A separate, intentionally-shallow shape from the full row returned
    by the POST: the boundary guard only ever asks "is this tenant
    suppressed right now?" — surfacing the full row here would invite
    consumers to start depending on ``updated_at`` and erode the cache
    invariant. A missing row is the same as "not suppressed", which is
    the standalone-OSS shape.
    """
    suppressed = await _svc.is_tenant_suppressed(tenant_id)
    return {"tenant_id": tenant_id, "is_suppressed": suppressed}
