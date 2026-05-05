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
