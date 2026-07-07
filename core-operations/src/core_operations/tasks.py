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
