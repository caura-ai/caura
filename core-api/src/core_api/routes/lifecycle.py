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
import weakref
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from common.env_utils import read_int_env
from common.events import (
    publish_archive_expired_request,
    publish_archive_stale_request,
    publish_crystallize_request,
    publish_entity_link_request,
    publish_insights_request,
    publish_purge_soft_deleted_request,
)
from common.events.lifecycle_publishers import (
    publish_embed_backfill_request,
    publish_forge_distill_request,
)
from common.events.lifecycle_purge_request import (
    MEMORY_RETENTION_MAX_DAYS,
    MEMORY_RETENTION_MIN_DAYS,
)
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.services.lifecycle_audit import audit_begin, resolve_publisher_kwargs
from core_api.services.tenants import (
    list_active_tenant_ids,
    list_tenants_with_purgeable_memories,
    list_tenants_with_skills_factory_enabled,
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
    # Periodic discovery insights. Consumer also lives in core-api and
    # short-circuits via the activity gate + opt-in org flag.
    "insights": publish_insights_request,
    # Periodic NULL-embedding re-embed sweep. Consumer is core-worker (it
    # owns ``core_worker.backfill``); it republishes one EMBED_REQUESTED per
    # row rather than embedding inline, so the work paces through the normal
    # consumer path instead of competing with live writes at full rate.
    #
    # PROVISION THE TOPIC BEFORE TRIGGERING THIS. ``memclaw.lifecycle.
    # embed-backfill-requested`` is Terraform-provisioned, and
    # ``PubSubEventBus.publish`` deliberately does not block on the publish
    # future, so a "topic not found" surfaces only in the SDK's background
    # thread. Triggering either route before infra lands therefore returns 200
    # with an ``audit_id`` whose row sits at ``pending`` forever, with no error
    # to read. ``embed_backfill_enabled`` gates the core-operations cron but
    # cannot gate this route — it is a core-operations setting — so the order
    # is: provision topic + subscription + DLQ, then flip the flag.
    "embed-backfill": publish_embed_backfill_request,
    # Skill Factory cron tick. Consumer also lives in core-api;
    # short-circuits via the ``org_settings.skills_factory.enabled``
    # tenant filter in ``_list_tenants_for_action`` — non-opted-in
    # tenants never appear in the fanout, so they pay zero per-tick
    # cost (no message published, no audit row written).
    "forge-distill": publish_forge_distill_request,
}

# Cap on concurrent per-org ``audit_begin + publish`` pairs. Each pair =
# 1 HTTP POST to core-storage-api + 1 Pub/Sub publish; without the cap, a
# deployment with thousands of orgs would fire that many simultaneous
# round-trips on a single cron tick.
#
# Tunable because the right number is a property of the OPERATOR's storage
# tier rather than of this code: it wants to stay under core-storage-api's
# warm request capacity (instances x per-instance concurrency), and that
# differs per deployment. 50 remains the default.
#
# See the ceiling arithmetic below before picking a value — the budget binds
# per worker PROCESS, so the deployment-wide figure is larger than this number.
#
# Read through ``read_int_env`` rather than a bare ``int(...)``, and that
# choice is about blast radius rather than tidiness. ``core_api.app`` imports
# this module unconditionally at startup, so anything raising at import time
# here stops every route serving — memories, search, auth — over one cron
# tunable. A mistyped ``5o`` or a stray ``0`` degrades to the default with a
# stderr warning instead. The helper's ``minimum`` already defaults to 1, and
# its docstring names this exact hazard: a semaphore-style cap reads 0 as
# "block forever", which would park every fanout with no error and no timeout.
_FANOUT_CONCURRENCY: int = read_int_env("LIFECYCLE_FANOUT_CONCURRENCY", 50)

# The budget is PER WORKER PROCESS, not per-request, and that distinction is
# the whole point of this block. Read the last paragraph before sizing it: per
# process is not per deployment.
#
# It used to be constructed inside the handler, so every in-flight fanout got
# a fresh budget of its own. The scheduler fires each action on the same cron
# minute — eight of them as of this writing — so the real ceiling was
# 8 x _FANOUT_CONCURRENCY simultaneous storage round-trips, while the comment
# above it claimed the cap kept the storage writer "never saturated by fanout
# traffic alone". That claim reasoned about a single fanout. Nothing bounded
# the aggregate, and the aggregate is what the writer actually sees.
#
# Measured 2026-09-15 on a deployment whose writer held 120 warm request slots
# against a ceiling of 8 x 50 = 400: the burst pushed it past capacity, the
# platform shed the overflow, and ``audit_begin`` raised for 38 distinct orgs
# across 5 actions. Those orgs' lifecycle actions did not run and left no
# audit row behind — so every downstream counter stayed self-consistent and
# the shortfall was invisible to all of them. A row that is never created
# cannot be counted as failed.
#
# WHAT THIS DOES NOT DO, and operators must size around it: the cap is not
# deployment-wide. core-api's Dockerfile runs uvicorn with ``--workers 2`` and
# the service scales horizontally, so the real ceiling against
# core-storage-api is ``instances x workers x _FANOUT_CONCURRENCY``. Each
# fanout POST lands on one worker, so this bounds the actions that happen to
# share a worker and nothing more — in the worst case, N actions landing on N
# distinct workers, it changes nothing at all. It is strictly better than a
# per-REQUEST budget, which bounded nothing even within a worker, but the
# honest headline is "predictable per worker", not "400 becomes 50". A true
# cross-process cap needs shared state (a Redis-backed semaphore) and is a
# separate change; until then, size this against one worker's share of the
# writer's warm capacity.
#
# Keyed by running loop rather than a bare module-level ``Semaphore``: a
# Semaphore binds to the first loop that awaits it, so a plain global raises
# "bound to a different event loop" in any suite that runs more than one.
# WeakKeyDictionary so a finished loop's entry is collected with it. uvloop is
# the production loop here (locked via the ``uvicorn[standard]`` extra and
# selected by uvicorn's default ``loop="auto"``), and its ``Loop`` is a Cython
# cdef class — but it DOES support weak references: verified against the
# pinned uvloop 0.22.1 that both ``weakref.ref(loop)`` and
# ``WeakKeyDictionary.__setitem__`` succeed. Recorded because the question is
# a reasonable one to have about a cdef class, and the answer is not obvious
# from reading this line.
_FANOUT_SEMAPHORES: MutableMapping[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _fanout_semaphore() -> asyncio.Semaphore:
    """Return this process's shared fanout budget for the running loop."""
    loop = asyncio.get_running_loop()
    sem = _FANOUT_SEMAPHORES.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_FANOUT_CONCURRENCY)
        _FANOUT_SEMAPHORES[loop] = sem
    return sem


async def _list_tenants_for_action(action: str) -> list[str]:
    """Discovery query for the fanout — purge is the odd action here:
    its target is orgs whose soft-deleted rows have aged past the
    retention window. The archive ``deleted_at IS NULL`` filter would
    silently drop the orgs we most need to run against (e.g. an org
    that 100%-soft-deleted its memories), so purge gets its own
    helper bounded to soft-deleted rows older than
    ``MEMORY_RETENTION_MAX_DAYS``.

    Each helper now reads through core-storage-api (Fix 2 Phase 1), so no
    core-api DB session is involved.
    """
    if action == "purge-soft-deleted":
        return await list_tenants_with_purgeable_memories()
    if action == "forge-distill":
        # Per-tick zero-cost for non-opted-in tenants: filter to orgs
        # that flipped ``skills_factory.enabled=true``. Any tenant
        # without an org_settings row (or with the flag false) is
        # excluded from the fanout entirely.
        return await list_tenants_with_skills_factory_enabled()
    return await list_active_tenant_ids()


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

    The org list is fetched up front (via core-storage-api) before the
    ``asyncio.gather`` fan-out; the fan-out itself holds no DB session, so a
    slow per-org Pub/Sub publish can't park a connection for the whole loop.
    """
    auth.enforce_admin()
    publisher = _resolve_publisher(action)

    org_ids = await _list_tenants_for_action(action)

    # Fan out concurrently under the PROCESS-WIDE budget (see
    # ``_fanout_semaphore``) so neither a deployment with N orgs nor N
    # actions firing on the same cron minute can exceed it.
    # ``return_exceptions=True`` keeps the
    # one-bad-org-must-not-abort-the-rest invariant.
    sem = _fanout_semaphore()

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


# A fanout completes in about a minute in production. 30 minutes is far
# past that, so a row still ``pending`` at this age was not slow -- its
# message was never published at all.
_RECONCILE_STRANDED_AFTER_MINUTES = 30
# Caps one sweep. A larger backlog drains oldest-first across successive
# sweeps rather than turning the reconcile into its own unbounded fanout.
_RECONCILE_MAX_ROWS = 200
# A row older than this has already been through at least two hourly sweeps
# and is STILL pending, so those sweeps did not work on it. Publishing is
# fire-and-forget (``PubSubEventBus.publish`` batches and does not await the
# delivery future, so a 403 on the topic never reaches this code), which
# means "the publish call returned" is not evidence of delivery. Row age is
# the only evidence available here, and it is sufficient: a repaired row
# leaves ``pending`` and stops being selected at all.
_RECONCILE_INEFFECTIVE_AFTER_MINUTES = 150


# MUST stay registered before ``POST /admin/lifecycle/{action}`` below. That
# route is a catch-all of the same three-segment shape, and Starlette matches
# in registration order, so moving this one after it would route these requests
# into ``trigger_lifecycle_action`` and answer with a confusing "unknown
# lifecycle action 'reconcile-stranded'" 404 instead of running the sweep --
# a silent misroute, since both are valid-looking POSTs to this prefix.
# ``test_reconcile_route_resolves_to_the_sweep_not_the_catch_all`` resolves an
# actual request against the router so a reorder fails loudly.
@router.post("/admin/lifecycle/reconcile-stranded")
async def reconcile_stranded_lifecycle_actions(
    auth: AuthContext = Depends(get_auth_context),
) -> dict:
    """Republish the message for every audit row the fanout stranded.

    ``_trigger_one`` writes the audit row BEFORE publishing, so that a
    failed publish leaves a visible ``pending`` row. Nothing ever acted
    on those rows: there is no redelivery, because the message was never
    published; no reconciler; no sweep. The work did not run, and the
    row records a job that never happened.

    This completes them. Each stranded row's message is republished
    under the SAME ``audit_id``, so the consumer finalizes the ORIGINAL
    row. Publishing a fresh row instead would leave the stranded one
    exactly as stuck as before while making the totals look healthier --
    the row that is wrong is the one that has to reach a terminal state.

    Safe to repeat, but not for the reason it first appears. The dedup
    gate only catches work that has already SUCCEEDED, so it does
    nothing for two deliveries racing before either finishes -- and a
    row can sit at ``pending`` because its message is merely queued
    (subscriptions here retain for seven days, so no age threshold
    separates "lost" from "slow"), which means a republish can land
    alongside a live original. What actually makes repetition safe is
    the claim: ``pending -> in_progress`` is a compare-and-swap, so only
    one delivery may start the primitive and the loser nacks. Sticky
    success then makes its retry a no-op.

    Restricted to ``triggered_by='core-operations'``. Manual triggers may
    have carried a ``fleet_id`` or a one-off ``retention_days`` that this
    table does not persist, so republishing one would run a different job
    than the row records.

    Runs under the SAME process-wide semaphore as the nightly fanout. A
    sweep with its own budget would be a second unbounded fanout against
    the storage writer -- the precise failure that stranded these rows.

    ``republish_attempted`` means the publish call returned without
    raising. It does NOT mean the message was delivered:
    ``PubSubEventBus.publish`` batches and deliberately does not await
    the delivery future, so publisher-side failures such as a 403 on an
    unprovisioned topic never reach this code. Naming the counter for
    what is actually known keeps a sweep that is achieving nothing from
    reading as a successful one. ``ineffective`` is that signal: rows
    this sweep republished which are already far past the point where
    they should have completed. It is derived from row age, the only
    evidence available here, and age cannot separate "an earlier sweep
    failed on this row" from "no sweep had run yet" -- true of the whole
    backlog on first deployment. Both call for the same investigation,
    so the counter reports the observation and does not assert a cause.
    """
    auth.enforce_admin()
    storage = get_storage_client()
    rows = await storage.list_stranded_lifecycle_audits(
        triggered_by="core-operations",
        older_than_minutes=_RECONCILE_STRANDED_AFTER_MINUTES,
        limit=_RECONCILE_MAX_ROWS,
    )
    if not rows:
        return {
            "stranded": 0,
            "republish_attempted": 0,
            "failed": 0,
            "unknown_action": 0,
            "ineffective": 0,
        }

    sem = _fanout_semaphore()

    async def _republish(row: dict) -> str:
        publisher = _ACTION_PUBLISHERS.get(row["action"])
        if publisher is None:
            # Nothing can ever republish this row. Left pending it would sit
            # at the head of every future oldest-first sweep and, once enough
            # accumulate, starve repairable rows out of the LIMIT window
            # entirely. Terminal failure is the truthful state: unlike an
            # undelivered message, this job is unrunnable, not merely
            # unreported. Finalizing also drops it from the partial index.
            #
            # Inside the semaphore: this is a write to core-storage-api like
            # any other. A batch of rows sharing one retired action would
            # otherwise fire up to ``_RECONCILE_MAX_ROWS`` concurrent PATCHes
            # at the writer -- the unbounded burst this sweep exists to avoid
            # recreating, arriving by the one path that skipped the budget.
            async with sem:
                await storage.update_lifecycle_audit_row(
                    row["audit_id"],
                    org_id=row["org_id"],
                    status="failure",
                    error_message=(
                        f"lifecycle action {row['action']!r} is no longer in "
                        "the publisher registry; the reconcile sweep cannot "
                        "republish it"
                    ),
                )
            return "unknown_action"
        async with sem:
            extra = await resolve_publisher_kwargs(row["action"], row["org_id"])
            await publisher(
                audit_id=row["audit_id"],
                org_id=row["org_id"],
                triggered_by=row["triggered_by"],
                # Always None for a fanout row: the cron path never passes
                # one, which is half of why only fanout rows are swept.
                fleet_id=None,
                **(extra or {}),
            )
        return "attempted"

    results = await asyncio.gather(
        *(_republish(row) for row in rows),
        return_exceptions=True,
    )

    republish_attempted = 0
    failed = 0
    unknown_action = 0
    ineffective = 0
    # Rows older than this were stranded long enough ago that, once the sweep
    # has been running, an earlier pass should already have repaired them.
    cutoff = datetime.now(UTC) - timedelta(minutes=_RECONCILE_INEFFECTIVE_AFTER_MINUTES)

    def _is_long_unrepaired(row: dict) -> bool:
        started = row.get("started_at")
        if not isinstance(started, str):
            return False
        try:
            return datetime.fromisoformat(started) < cutoff
        except ValueError:
            return False

    for row, outcome in zip(rows, results, strict=True):
        if isinstance(outcome, BaseException):
            # Leave the row stranded rather than finalizing it: the next
            # sweep retries, and a row marked failure here would report the
            # sweep's own problem as the lifecycle job's outcome.
            logger.exception(
                "lifecycle reconcile: republish failed; row stays stranded for the next sweep",
                exc_info=outcome,
                extra={
                    "action": row.get("action"),
                    "org_id": row.get("org_id"),
                    "audit_id": row.get("audit_id"),
                },
            )
            failed += 1
        elif outcome == "unknown_action":
            unknown_action += 1
            logger.warning(
                "lifecycle reconcile: stranded row names an unknown action; "
                "finalized as failure so it stops blocking the sweep",
                extra={"action": row.get("action"), "audit_id": row.get("audit_id")},
            )
        else:
            republish_attempted += 1
            # Only rows this sweep actually published for. A row finalized as
            # ``unknown_action`` above is terminal now, not stuck, and one that
            # raised is already reported as ``failed`` -- counting either here
            # would report the same row twice under two different causes.
            if _is_long_unrepaired(row):
                ineffective += 1

    if ineffective:
        # Deliberately states what was observed, not what caused it.
        # ``republish_attempted`` cannot distinguish a delivered message from
        # one the Pub/Sub SDK dropped on a background thread, and row age
        # cannot distinguish "an earlier sweep failed on this row" from "no
        # sweep had ever run yet" -- which is every row in the backlog on the
        # first deployment. Both readings warrant the same operator action
        # (find out why these rows are not completing), so the line reports
        # the fact and leaves the cause open rather than asserting a history
        # it cannot see.
        logger.error(
            "lifecycle reconcile: rows republished this sweep are far past the "
            "point where they should have completed; if a previous sweep ran, "
            "its publishes did not land",
            extra={
                "ineffective": ineffective,
                "older_than_minutes": _RECONCILE_INEFFECTIVE_AFTER_MINUTES,
            },
        )

    logger.info(
        "lifecycle reconcile swept stranded rows",
        extra={
            "stranded": len(rows),
            "republish_attempted": republish_attempted,
            "failed": failed,
            "unknown_action": unknown_action,
            "ineffective": ineffective,
        },
    )
    return {
        "stranded": len(rows),
        "republish_attempted": republish_attempted,
        "failed": failed,
        "unknown_action": unknown_action,
        "ineffective": ineffective,
    }


@router.get("/admin/lifecycle/audits/summary")
async def lifecycle_audit_summary(
    since_hours: int = 30,
    triggered_by: str | None = None,
    auth: AuthContext = Depends(get_auth_context),
) -> dict:
    """Return recent lifecycle status counts for deployment health checks.

    The response is aggregate-only and uncapped, so a large fanout cannot push
    an early failure out of view. Admin auth is required because the result
    spans every organization in the deployment.
    """
    auth.enforce_admin()
    if since_hours < 1 or since_hours > 168:
        raise HTTPException(
            status_code=422,
            detail="'since_hours' must be in [1, 168] (hours)",
        )
    return await get_storage_client().get_lifecycle_audit_summary(
        since_hours=since_hours,
        triggered_by=triggered_by,
    )


@router.get("/admin/lifecycle/audits/{audit_id}")
async def get_lifecycle_audit(
    audit_id: int,
    org_id: str,
    auth: AuthContext = Depends(get_auth_context),
) -> dict:
    """Return one audit row for an active canary's exact message."""
    auth.enforce_admin()
    row = await get_storage_client().get_lifecycle_audit_row(
        audit_id,
        org_id=org_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"lifecycle audit {audit_id} not found")
    return row


@router.get("/admin/lifecycle/embedding-coverage")
async def embedding_coverage_all_tenants(
    auth: AuthContext = Depends(get_auth_context),
) -> dict:
    """How many live memories are still unembedded, per tenant.

    This is the standing answer to "is the embed-backfill sweep keeping up",
    and it lives here because that sweep is what acts on the number. Before
    this, the count existed only inside the VPC: both storage services are
    internal-ingress, no metric carried it, and reading it meant an AlloyDB
    Auth Proxy session and a hand-written COUNT — so in practice nobody
    measured it, including while turning the sweep on.

    Admin-only, and cross-tenant, which is the whole point: an operator asking
    this question is asking it about the deployment, not about one tenant.
    ``enforce_admin`` is what keeps the aggregate off a tenant credential — the
    admin key resolves to ``tenant_id=None``, so there is no tenant scope to
    fall back on and a missing gate would expose every tenant's row counts.
    Counts and tenant ids only; no memory content.

    A GET beside the ``POST /admin/lifecycle/{action}`` catch-all below: the
    methods differ, so the path-param route cannot shadow this one.
    """
    auth.enforce_admin()
    coverage = await get_storage_client().get_embedding_coverage_all()
    logger.info(
        "embedding coverage sampled",
        extra={
            "total_active": coverage.get("total_active"),
            "missing_embeddings": coverage.get("missing_embeddings"),
            "tenants_with_missing": coverage.get("tenants_with_missing"),
            "stale_embeddings": coverage.get("stale_embeddings"),
            "unknown_provenance": coverage.get("unknown_provenance"),
            # Should be 0. See ``run_embedding_coverage_tick``, which alerts.
            "missing_provenance": coverage.get("missing_provenance"),
        },
    )
    return coverage


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
