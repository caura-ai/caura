"""Admin endpoints for OSS scheduled lifecycle operations (CAURA-655).

Two flavours per action — ``archive-expired`` and ``archive-stale``:

* ``POST /admin/lifecycle/fanout/<action>`` — cron-fired entry point.
  Lists every tenant with live memories, pre-publishes one audit row
  per tenant, and publishes one Pub/Sub message per tenant.

* ``POST /admin/lifecycle/<action>`` — manual single-tenant trigger.
  Body ``{"org_id": "..."}``. Same downstream code path as the fanout
  loop body — both converge at one ``audit_begin + publish`` pair.

Auth: admin-key only (``auth.enforce_admin``). The fanout route is
called by ``core-operations`` over the network with the configured
``CORE_API_ADMIN_API_KEY``; the manual route is for operator curl /
admin UI.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request

from common.events import (
    publish_archive_expired_request,
    publish_archive_stale_request,
    publish_crystallize_request,
    publish_entity_link_request,
    publish_purge_soft_deleted_request,
)
from common.events.lifecycle_purge_request import (
    MEMORY_RETENTION_MAX_DAYS,
    MEMORY_RETENTION_MIN_DAYS,
)
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.db.session import async_session
from core_api.services.lifecycle_audit import audit_begin, resolve_publisher_kwargs
from core_api.services.tenants import (
    list_active_tenant_ids,
    list_tenants_with_purgeable_memories,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Admin", "Lifecycle"])

_PublisherFn = Callable[..., Awaitable[None]]
_ACTION_PUBLISHERS: dict[str, _PublisherFn] = {
    "archive-expired": publish_archive_expired_request,
    "archive-stale": publish_archive_stale_request,
    "purge-soft-deleted": publish_purge_soft_deleted_request,
    # CAURA-657: pipeline ops — consumer is core-api itself.
    "crystallize": publish_crystallize_request,
    "entity-link": publish_entity_link_request,
}

# Cap on concurrent per-org ``audit_begin + publish`` pairs in the
# fanout loop. Each pair = 1 HTTP POST to core-storage-api + 1 Pub/Sub
# publish; without the cap, a deployment with thousands of orgs would
# fire that many simultaneous round-trips on a single cron tick. 50 is
# a generous default — enough that small deploys never queue, low
# enough that the storage-writer pool is never saturated by fanout
# traffic alone (the same pool serves the live request path).
_FANOUT_CONCURRENCY = 50


async def _list_tenants_for_action(action: str, db) -> list[str]:
    """Discovery query for the fanout — purge is the odd action here:
    its target is orgs whose soft-deleted rows have aged past the
    retention window. The archive ``deleted_at IS NULL`` filter would
    silently drop the orgs we most need to run against (e.g. an org
    that 100%-soft-deleted its memories), so purge gets its own
    helper bounded to soft-deleted rows older than
    ``MEMORY_RETENTION_MAX_DAYS``.
    """
    if action == "purge-soft-deleted":
        return await list_tenants_with_purgeable_memories(db)
    return await list_active_tenant_ids(db)


def _resolve_publisher(action: str) -> _PublisherFn:
    publisher = _ACTION_PUBLISHERS.get(action)
    if publisher is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown lifecycle action {action!r}; valid: {sorted(_ACTION_PUBLISHERS)}",
        )
    return publisher


async def _trigger_one(
    *,
    action: str,
    org_id: str,
    triggered_by: str,
    publisher: _PublisherFn,
    fleet_id: str | None = None,
    extra_kwargs: dict | None = None,
) -> int:
    """Pre-publish the audit row, then publish the per-org Pub/Sub
    message. Returns the new audit_id.

    The audit row goes out FIRST so a publish failure leaves a
    ``pending`` row pointing at the operator's request — observable as
    a row that never advances. Reverse ordering would let the consumer
    receive a message referencing an id that doesn't exist.

    ``extra_kwargs`` carries action-specific publisher kwargs (e.g.
    ``retention_days`` for purge). The publisher's Pydantic payload
    enforces ``extra='forbid'`` so a wrong-action kwarg fails fast at
    publish rather than producing a silent no-op message.
    """
    storage = get_storage_client()
    audit_id = await audit_begin(
        storage,
        action=action,
        org_id=org_id,
        triggered_by=triggered_by,
    )
    await publisher(
        audit_id=audit_id,
        org_id=org_id,
        triggered_by=triggered_by,
        fleet_id=fleet_id,
        **(extra_kwargs or {}),
    )
    return audit_id


@router.post("/admin/lifecycle/fanout/{action}")
async def fanout_lifecycle_action(
    action: str,
    auth: AuthContext = Depends(get_auth_context),
) -> dict:
    """Cron entry point — publish one message per active org.

    Caller is ``core-operations`` (``triggered_by='core-operations'``).
    Returns ``{"action", "published", "failed"}`` — counts only, no
    per-org id list, so the response stays bounded at scale.

    The DB session is opened locally and released BEFORE the
    ``asyncio.gather`` fan-out so a slow per-org Pub/Sub publish can't
    park a connection for the whole loop. The fan-out only needs the
    org list, not the session.
    """
    auth.enforce_admin()
    publisher = _resolve_publisher(action)

    async with async_session() as db:
        org_ids = await _list_tenants_for_action(action, db)

    # Fan out concurrently with a semaphore cap (see _FANOUT_CONCURRENCY)
    # so a deployment with N orgs doesn't fire N simultaneous storage
    # round-trips. ``return_exceptions=True`` keeps the
    # one-bad-org-must-not-abort-the-rest invariant.
    sem = asyncio.Semaphore(_FANOUT_CONCURRENCY)

    async def _bounded_trigger(org_id: str) -> int:
        async with sem:
            extra = await resolve_publisher_kwargs(action, org_id)
            return await _trigger_one(
                action=action,
                org_id=org_id,
                triggered_by="core-operations",
                publisher=publisher,
                extra_kwargs=extra or None,
            )

    results = await asyncio.gather(
        *(_bounded_trigger(org_id) for org_id in org_ids),
        return_exceptions=True,
    )

    published = 0
    failed = 0
    for org_id, outcome in zip(org_ids, results, strict=True):
        if isinstance(outcome, BaseException):
            logger.exception(
                "lifecycle fanout: failed to trigger one org; continuing",
                exc_info=outcome,
                extra={"action": action, "org_id": org_id},
            )
            failed += 1
            continue
        published += 1

    logger.info(
        "lifecycle fanout dispatched",
        extra={
            "action": action,
            "org_count": len(org_ids),
            "published": published,
            "failed": failed,
        },
    )
    return {"action": action, "published": published, "failed": failed}


@router.post("/admin/lifecycle/{action}")
async def trigger_lifecycle_action(
    action: str,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> dict:
    """Manual single-org trigger.

    Body: ``{"org_id": "...", "fleet_id": "..." (optional)}``. Same
    downstream as the fanout-loop body. ``triggered_by`` records who
    initiated: ``manual:<user-id>`` if the auth context carries a user,
    else ``manual:admin-key`` for raw curl.
    """
    auth.enforce_admin()
    publisher = _resolve_publisher(action)

    # ``request.json()`` raises ``JSONDecodeError`` on a malformed body;
    # without the guard FastAPI's catch-all maps it to 500. Surface as
    # 422 so the caller can self-diagnose.
    try:
        body: dict = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail="request body must be valid JSON",
        ) from exc

    org_id = body.get("org_id")
    if not isinstance(org_id, str) or not org_id:
        raise HTTPException(
            status_code=422,
            detail="'org_id' must be a non-empty string",
        )
    fleet_id = body.get("fleet_id")
    if fleet_id is not None and not isinstance(fleet_id, str):
        raise HTTPException(
            status_code=422,
            detail="'fleet_id' must be a string when provided",
        )

    # Manual route lets the operator override per-org settings via
    # body keys (dry-running a different ``retention_days`` without
    # touching the persisted setting). Without an override, fall back
    # to the same per-action resolver the cron path uses. Range
    # validation is delegated to the publisher's Pydantic payload —
    # single source of truth with the storage-side primitive.
    extra_kwargs = await resolve_publisher_kwargs(action, org_id)
    body_retention = body.get("retention_days")
    if body_retention is not None:
        # Gate on action FIRST: archive-expired / archive-stale
        # publishers don't accept ``retention_days``, so an unfiltered
        # body kwarg would propagate through ``extra_kwargs`` and cause
        # a TypeError → 500 inside ``publisher(**extra_kwargs)``. Worse,
        # ``audit_begin`` runs before that splat, so each such request
        # would also leave a ``pending`` audit row that never advances.
        # Fail at the route boundary with a 422 before ``audit_begin``.
        if action != "purge-soft-deleted":
            raise HTTPException(
                status_code=422,
                detail="'retention_days' is only valid for the 'purge-soft-deleted' action",
            )
        # ``isinstance(True, int)`` is True; carve bools out so a body
        # of ``{"retention_days": true}`` is rejected loudly.
        if not isinstance(body_retention, int) or isinstance(body_retention, bool):
            raise HTTPException(
                status_code=422,
                detail="'retention_days' must be an integer when provided",
            )
        # Range-check at the route boundary — without it, an out-of-
        # range value reaches the publisher's Pydantic payload and
        # raises ValidationError, which the global catch-all maps to
        # 500 instead of the 422 the caller would expect.
        if not (MEMORY_RETENTION_MIN_DAYS <= body_retention <= MEMORY_RETENTION_MAX_DAYS):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"'retention_days' must be in [{MEMORY_RETENTION_MIN_DAYS}, {MEMORY_RETENTION_MAX_DAYS}]"
                ),
            )
        extra_kwargs["retention_days"] = body_retention

    triggered_by = f"manual:{auth.user_id}" if auth.user_id else "manual:admin-key"
    audit_id = await _trigger_one(
        action=action,
        org_id=org_id,
        triggered_by=triggered_by,
        publisher=publisher,
        fleet_id=fleet_id,
        extra_kwargs=extra_kwargs or None,
    )
    return {"action": action, "org_id": org_id, "audit_id": audit_id}
