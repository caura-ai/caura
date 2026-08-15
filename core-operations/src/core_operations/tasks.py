"""Scheduled task callables for core-operations (CAURA-655).

Each tick is intentionally dumb: POST to core-api's fanout endpoint
with the configured admin key. core-api owns org enumeration, audit
pre-publish, and Pub/Sub publication; this service is just the cron
trigger so it doesn't need DB access or org concepts.

Each cron registration is its own action — never a single umbrella
``run-cycle`` — so an outage on one operation can't silently take down
the others, and per-action audit rows stay independent.
"""

from __future__ import annotations

import logging

import httpx

from core_operations.config import settings

logger = logging.getLogger(__name__)

# Per-tenant embedding-coverage lines emitted per tick. The deployment-wide
# total is always logged; this bounds only the per-tenant detail, which is
# ordered worst-first so the cap drops the tenants nobody would act on. Sized
# so one tick stays a readable handful of lines rather than one per tenant.
_COVERAGE_TENANT_LOG_CAP = 20


async def _fire_fanout(action: str) -> None:
    """POST ``/admin/lifecycle/fanout/<action>``. A non-2xx response
    logs and returns; the scheduler retries on the next tick, so
    re-raising would just produce duplicate stack traces.
    """
    url = f"{settings.core_api_url.rstrip('/')}/api/v1/admin/lifecycle/fanout/{action}"
    headers: dict[str, str] = {}
    if settings.core_api_admin_api_key:
        headers["X-API-Key"] = settings.core_api_admin_api_key
    else:
        # Missing admin key would 401 every fanout silently — log so the
        # operator can see why all subsequent ticks fail.
        logger.warning(
            "core-operations: CORE_API_ADMIN_API_KEY unset; fanout will be unauthorised",
            extra={"action": action},
        )

    timeout = httpx.Timeout(settings.storage_http_timeout_s)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(url, headers=headers)
        except httpx.HTTPError:
            logger.exception(
                "lifecycle fanout POST failed",
                extra={"action": action, "url": url},
            )
            return
    if resp.status_code >= 400:
        logger.error(
            "lifecycle fanout returned non-2xx; will retry next tick",
            extra={
                "action": action,
                "status_code": resp.status_code,
                "body": resp.text[:500],
            },
        )
        return
    body = resp.json()
    logger.info(
        "lifecycle fanout fired",
        extra={
            "action": action,
            "published": body.get("published"),
        },
    )


async def run_archive_expired_tick() -> None:
    await _fire_fanout("archive-expired")


async def run_archive_stale_tick() -> None:
    await _fire_fanout("archive-stale")


async def run_purge_soft_deleted_tick() -> None:
    """Hard-delete soft-deleted memories older than each org's
    ``lifecycle.memory_retention_days`` setting. The per-org settings
    snapshot happens inside the core-api fanout endpoint, not here —
    core-operations stays oblivious of org concepts.
    """
    await _fire_fanout("purge-soft-deleted")


async def run_crystallize_tick() -> None:
    """CAURA-657: trigger crystallization per active org. Consumer
    side runs in core-api (pipeline machinery isn't reachable from
    core-worker); a 23-hour dedup gate inside the consumer skips orgs
    that succeeded within the window.
    """
    await _fire_fanout("crystallize")


async def run_entity_link_tick() -> None:
    """CAURA-657: trigger entity-link discovery per active org. Same
    consumer-side dedup window as crystallize.
    """
    await _fire_fanout("entity-link")


async def run_embed_backfill_tick() -> None:
    """Re-embed rows whose embedding is still NULL, one message per org.

    The only periodic self-healing path for memories that never got an
    embedding scheduled — normal writes already republish EMBED_REQUESTED
    themselves, so this exists for the rows that fell through entirely.
    """
    await _fire_fanout("embed-backfill")


async def run_embedding_coverage_tick() -> None:
    """Log embedding coverage per tenant so the backlog is observable.

    Reads ``GET /admin/lifecycle/embedding-coverage`` on core-api and emits one
    structured line per tenant plus a deployment-wide total. This is the whole
    point of the tick: there is no metrics client anywhere in the stack —
    observability is structlog into Datadog logs — so a periodic log line IS
    the metric, and without it the count is invisible outside a manual AlloyDB
    session.

    Read-only and idempotent, so the cadence is a free choice; hourly gives a
    curve with enough resolution to see whether the nightly sweep actually
    drains the backlog, which a daily sample taken near the sweep cannot.

    Per-tenant lines are capped: a deployment with thousands of tenants would
    otherwise turn one tick into thousands of log lines. The total is always
    emitted, and the per-tenant detail is worst-first, so the cap drops the
    tenants nobody would act on. The cap is logged when it bites — a silent
    truncation would read as "only these tenants have gaps".
    """
    url = f"{settings.core_api_url.rstrip('/')}/api/v1/admin/lifecycle/embedding-coverage"
    headers: dict[str, str] = {}
    if settings.core_api_admin_api_key:
        headers["X-API-Key"] = settings.core_api_admin_api_key
    else:
        logger.warning(
            "core-operations: CORE_API_ADMIN_API_KEY unset; embedding-coverage sample will be unauthorised",
        )

    timeout = httpx.Timeout(settings.storage_http_timeout_s)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.get(url, headers=headers)
        except httpx.HTTPError:
            logger.exception("embedding-coverage GET failed", extra={"url": url})
            return
    if resp.status_code >= 400:
        logger.error(
            "embedding-coverage returned non-2xx; will retry next tick",
            extra={"status_code": resp.status_code, "body": resp.text[:500]},
        )
        return
    try:
        coverage = resp.json()
    except ValueError:
        logger.error(
            "embedding-coverage returned non-JSON; will retry next tick",
            extra={"body": resp.text[:200]},
        )
        return

    tenants = coverage.get("tenants") or []
    logger.info(
        "embedding coverage total",
        extra={
            "total_active": coverage.get("total_active"),
            "missing_embeddings": coverage.get("missing_embeddings"),
            "tenants_with_missing": coverage.get("tenants_with_missing"),
            # A vector computed from text the row no longer holds. Tracked
            # separately from ``missing`` because the nightly sweep CANNOT
            # repair it — the column is non-NULL, so the sweep never sees the
            # row. A rising number here means silently degraded recall.
            "stale_embeddings": coverage.get("stale_embeddings"),
            "tenants_with_stale": coverage.get("tenants_with_stale"),
            # Embedded before provenance existed: undetermined, not damaged.
            # Expected to fall over time; it is a measurement gap closing, so
            # do not alert on it.
            "unknown_provenance": coverage.get("unknown_provenance"),
            "tenant_count": len(tenants),
        },
    )
    # A tenant is worth a line if it has EITHER defect. Filtering on missing
    # alone would hide a tenant whose rows are all embedded but wrong — the
    # exact case this release makes visible.
    reported = [t for t in tenants if t.get("missing_embeddings") or t.get("stale_embeddings")][
        :_COVERAGE_TENANT_LOG_CAP
    ]
    for tenant in reported:
        logger.info(
            "embedding coverage tenant",
            extra={
                "tenant_id": tenant.get("tenant_id"),
                "total_active": tenant.get("total_active"),
                "missing_embeddings": tenant.get("missing_embeddings"),
                "stale_embeddings": tenant.get("stale_embeddings"),
                "unknown_provenance": tenant.get("unknown_provenance"),
                "coverage_pct": tenant.get("coverage_pct"),
            },
        )
    affected = sum(1 for t in tenants if t.get("missing_embeddings") or t.get("stale_embeddings"))
    if affected > len(reported):
        logger.info(
            "embedding coverage per-tenant lines truncated",
            extra={"reported": len(reported), "tenants_affected": affected},
        )


async def run_insights_tick() -> None:
    """Trigger insights discovery per active org. Same shape as the
    crystallize / entity-link ticks — POST the fanout endpoint and
    let core-api enumerate orgs + publish per-org events. The
    consumer is opt-in (``auto_insights_enabled`` default off) and
    short-circuits via an activity gate when the corpus hasn't grown
    since the last insights run, so firing daily is safe even for
    quiet tenants.
    """
    await _fire_fanout("insights")


async def _fire_agent_digest(period: str) -> None:
    """POST ``/admin/reports/agent-digest/run?period=<period>``. Unlike the
    lifecycle fanout, digest generation runs INLINE in core-api (no Pub/Sub), so
    this trigger has its own endpoint. Non-2xx logs and returns — the scheduler
    retries next tick.
    """
    url = f"{settings.core_api_url.rstrip('/')}/api/v1/admin/reports/agent-digest/run?period={period}"
    headers: dict[str, str] = {}
    if settings.core_api_admin_api_key:
        headers["X-API-Key"] = settings.core_api_admin_api_key
    else:
        logger.warning(
            "core-operations: CORE_API_ADMIN_API_KEY unset; agent-digest run will be unauthorised",
            extra={"period": period},
        )

    # Generation is a long inline job (LLM per agent across opted-in orgs), so
    # give it a generous timeout rather than the short storage default.
    timeout = httpx.Timeout(settings.agent_digest_http_timeout_s)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(url, headers=headers)
        except httpx.HTTPError:
            logger.exception("agent-digest POST failed", extra={"period": period, "url": url})
            return
    if resp.status_code >= 400:
        logger.error(
            "agent-digest run returned non-2xx; will retry next tick",
            extra={"period": period, "status_code": resp.status_code, "body": resp.text[:500]},
        )
        return
    try:
        summary = resp.json()
    except Exception:
        summary = resp.text[:200]
    logger.info("agent-digest run fired", extra={"period": period, "summary": summary})


async def run_agent_digest_tick() -> None:
    """Nightly per-agent activity digest generation (daily window). core-api
    enumerates opted-in orgs and generates inline; a tenant that hasn't opted in
    pays zero cost. Safe to fire daily."""
    await _fire_agent_digest("day")


async def run_agent_digest_weekly_tick() -> None:
    """Weekly per-agent digest (period=week). Same trigger as the daily tick,
    fired once a week so the previous full Mon-Mon window gets summarized."""
    await _fire_agent_digest("week")


async def run_interviewer_schedule_tick() -> None:
    """Queue due ``interview_request`` fleet commands (Interviewer Phase 1).

    POSTs ``/admin/interview/schedule/run`` on core-api, which enumerates
    orgs with ``interviewer.enabled`` and queues at most one pending command
    per live node, gated by each node's watermark vs the tenant's
    ``period_hours``. Firing hourly is safe: a tenant that hasn't opted in
    pays zero cost, and dueness gating makes the tick idempotent.
    """
    url = f"{settings.core_api_url.rstrip('/')}/api/v1/admin/interview/schedule/run"
    headers: dict[str, str] = {}
    if settings.core_api_admin_api_key:
        headers["X-API-Key"] = settings.core_api_admin_api_key
    else:
        logger.warning(
            "core-operations: CORE_API_ADMIN_API_KEY unset; interviewer schedule run will be unauthorised",
        )

    # Scheduling is queue-only (no LLM work inline) — the storage default
    # timeout is plenty.
    timeout = httpx.Timeout(settings.storage_http_timeout_s)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(url, headers=headers)
        except httpx.HTTPError:
            logger.exception("interviewer-schedule POST failed", extra={"url": url})
            return
    if resp.status_code >= 400:
        logger.error(
            "interviewer-schedule run returned non-2xx; will retry next tick",
            extra={"status_code": resp.status_code, "body": resp.text[:500]},
        )
        return
    try:
        summary = resp.json()
    except Exception:
        summary = resp.text[:200]
    logger.info("interviewer-schedule run fired", extra={"summary": summary})
