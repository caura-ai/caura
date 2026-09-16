"""Ask core-api whether this replica owns the current scheduled tick.

The scheduler is an in-process asyncio loop and this service holds no
datastore, so replicas cannot elect a leader among themselves. core-api
can — it owns the Redis behind ``/admin/scheduler/tick-lease`` — and
answering there also makes the guarantee caller-agnostic: it covers N
replicas, a retry, and an operator firing the same tick by hand.

Everything here fails OPEN. See ``Scheduler._claim`` for why that is the
design rather than a concession: denying a tick because the coordinator
was unreachable converts a transient outage into a silently skipped
nightly sweep, which is worse than the duplicate fire being prevented.
"""

from __future__ import annotations

import logging

import httpx

from core_operations.config import settings

logger = logging.getLogger(__name__)

# Deliberately NOT ``core_api_http_timeout_s`` (60s, sized to outlast
# core-api's own 45s request budget for the fanout work itself). This is a
# pre-flight question asked immediately before the tick fires, so its
# timeout is part of the tick's own latency: a lease that took 60s to
# answer would delay the work it is guarding by a minute. A Redis SETNX
# behind an already-warm service answers in milliseconds; seconds of
# allowance is generous, and exceeding it means fail-open anyway.
_LEASE_TIMEOUT_S = 5.0


async def claim_tick_lease(task: str, ttl_s: float) -> bool:
    """Return True if this process may run ``task``'s current tick.

    Returns True on every failure path — unreachable core-api, non-2xx,
    unparseable body, missing field. The only way to get False is an
    explicit ``{"granted": false}``, i.e. another replica demonstrably
    holds this tick.
    """
    url = f"{settings.core_api_url.rstrip('/')}/api/v1/admin/scheduler/tick-lease"
    headers: dict[str, str] = {}
    if settings.core_api_admin_api_key:
        headers["X-API-Key"] = settings.core_api_admin_api_key
    else:
        # Same posture as ``_fire_fanout``: an unset key 401s every call.
        # Without this line the estate would look like it were coordinating
        # while every lease silently failed open, which is the worst of
        # both — no guard, and no sign that the guard is absent.
        logger.warning(
            "core-operations: CORE_API_ADMIN_API_KEY unset; tick leases will "
            "be unauthorised and every tick will run unguarded",
            extra={"task": task},
        )

    timeout = httpx.Timeout(_LEASE_TIMEOUT_S)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json={"task": task, "ttl_s": ttl_s})
    except httpx.HTTPError:
        logger.warning(
            "tick lease request failed; running the tick unguarded",
            exc_info=True,
            extra={"task": task, "url": url},
        )
        return True

    if resp.status_code >= 400:
        # At warning rather than error: this degrades to today's
        # behaviour, it does not break the tick. A 422 here would mean the
        # task name or TTL this service computed is not one core-api
        # accepts, which is a real bug worth seeing but not worth skipping
        # the nightly work over.
        logger.warning(
            "tick lease returned non-2xx; running the tick unguarded",
            extra={
                "task": task,
                "status_code": resp.status_code,
                "body": resp.text[:500],
            },
        )
        return True

    try:
        granted = resp.json().get("granted")
    except Exception:
        logger.warning(
            "tick lease body was not JSON; running the tick unguarded",
            exc_info=True,
            extra={"task": task},
        )
        return True

    # Anything that is not exactly ``false`` runs. A missing or non-bool
    # ``granted`` means core-api answered something this version does not
    # understand, which is a reason to be permissive, not to skip work.
    if granted is False:
        return False
    if not isinstance(granted, bool):
        logger.warning(
            "tick lease answered a non-boolean 'granted'; running unguarded",
            extra={"task": task, "granted": repr(granted)},
        )
    return True
