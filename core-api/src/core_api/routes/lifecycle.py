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
)
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.db.session import async_session
from core_api.services.lifecycle_audit import audit_begin
from core_api.services.tenants import list_active_tenant_ids

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Admin", "Lifecycle"])

# Whitelist of action names → publisher.  CAURA-657 will extend with
# ``crystallize`` and ``entity-link`` once their consumers exist.
_PublisherFn = Callable[..., Awaitable[None]]
_ACTION_PUBLISHERS: dict[str, _PublisherFn] = {
    "archive-expired": publish_archive_expired_request,
    "archive-stale": publish_archive_stale_request,
}

# Cap on concurrent per-org ``audit_begin + publish`` pairs in the
# fanout loop. Each pair = 1 HTTP POST to core-storage-api + 1 Pub/Sub
# publish; without the cap, a deployment with thousands of orgs would
# fire that many simultaneous round-trips on a single cron tick. 50 is
# a generous default — enough that small deploys never queue, low
# enough that the storage-writer pool is never saturated by fanout
# traffic alone (the same pool serves the live request path).
_FANOUT_CONCURRENCY = 50


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
) -> int:
    """Pre-publish the audit row, then publish the per-org Pub/Sub
    message. Returns the new audit_id.

    The audit row goes out FIRST so a publish failure leaves a
    ``pending`` row pointing at the operator's request — observable as
    a row that never advances. Reverse ordering would let the consumer
    receive a message referencing an id that doesn't exist.
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
        org_ids = await list_active_tenant_ids(db)

    # Fan out concurrently with a semaphore cap (see _FANOUT_CONCURRENCY)
    # so a deployment with N orgs doesn't fire N simultaneous storage
    # round-trips. ``return_exceptions=True`` keeps the
    # one-bad-org-must-not-abort-the-rest invariant: per-iteration try/
    # except in a serial loop did the same job, but the cron tick used
    # to scale O(N) in wall time * per-org publish latency. The cron
    # re-runs on the configured cadence so a partial failure here is
    # always recoverable on the next tick.
    sem = asyncio.Semaphore(_FANOUT_CONCURRENCY)

    async def _bounded_trigger(org_id: str) -> int:
        async with sem:
            return await _trigger_one(
                action=action,
                org_id=org_id,
                triggered_by="core-operations",
                publisher=publisher,
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

    triggered_by = f"manual:{auth.user_id}" if auth.user_id else "manual:admin-key"
    audit_id = await _trigger_one(
        action=action,
        org_id=org_id,
        triggered_by=triggered_by,
        publisher=publisher,
        fleet_id=fleet_id,
    )
    return {"action": action, "org_id": org_id, "audit_id": audit_id}
