"""Boundary guard for soft-deleted org tenants (CAURA-694).

When an enterprise org is soft-deleted, ``platform-admin-api`` publishes
``memclaw.org.suppression-changed`` with the list of tenant_ids the
action covers. Core-worker mirrors that into
``public.tenant_suppression`` (CAURA-694 storage migration). This module
is the synchronous read side: core-api's auth layer asks
:func:`is_tenant_suppressed` on every authenticated request and 403s
when the answer is ``True``.

Why a short TTL cache:

  - core-api authenticates **every** request — on the recall / read /
    write paths that's the hottest dependency we own. A storage
    round-trip per request would double request-side latency.
  - The TTL bound (30 s) is the same shape the auth-api uses for the
    enterprise org-deleted check (CAURA-690): operators who soft-delete
    an org accept up to that much latency on the access cut-off, in
    exchange for the per-request cost. The synchronous auth-api gate
    (CAURA-690) already blocks JWT/cookie/API-key login paths immediately;
    this cache only governs the in-flight request window for callers who
    bypass auth-api (raw API-key path, OSS standalone).

Fail-open on transport error: a storage outage MUST NOT 403 every
request — the alternative is a self-inflicted DOS of core-api on
storage flaps. We log the failure and treat the tenant as live until
the cache window expires. The keystone-quality guarantee is therefore
"suppressed-tenant requests are blocked, **modulo a brief window on
storage outage**" — acceptable because the durable hard-delete sweep
(CAURA-691 / -692) and the auth-api gate (CAURA-690) are the real
defence-in-depth lines.
"""

from __future__ import annotations

import logging
import time

from core_api.clients.storage_client import get_storage_client

logger = logging.getLogger(__name__)

# Mirror the auth-api ``_is_org_deleted`` cache window (CAURA-690).
# Operators who soft-delete an org accept up to this much latency on
# the access cut-off, and the synchronous auth-api gate already covers
# the user-facing login flow — this cache only governs the bypass paths
# (raw API-key, OSS standalone) within the window.
_TTL_SECONDS: float = 30.0

# Shorter TTL written on storage failure (PR #244 bot review round 1,
# 🟠 High). Without this, the failure path returned ``False`` WITHOUT
# writing to the cache, so every subsequent authenticated request hit
# storage again during an outage — a thundering herd that turns a
# brief blip into a self-amplifying retry storm. 5 s gives the cache
# time to absorb the load while still self-healing fast once storage
# recovers (vs. waiting the full 30 s success-TTL).
_ERROR_TTL_SECONDS: float = 5.0

# Module-level cache: ``{tenant_id: (is_suppressed, monotonic_expires_at)}``.
# A dict is cheap; entries are evicted on write (see ``_prune_expired``
# below) so a long-running process with high tenant turnover doesn't
# leak memory by accumulating expired keys forever. Bot review round 2
# on PR #244.
_cache: dict[str, tuple[bool, float]] = {}


def _prune_expired(now: float) -> None:
    """Drop ``_cache`` entries whose TTL has passed.

    Called immediately before each write so the per-call cost is
    proportional to the number of currently-cached tenants (bounded
    by distinct tenants seen in the last ``_TTL_SECONDS`` window) and
    expired keys never linger. Building the key list before mutating
    avoids "dictionary changed size during iteration" — Python doesn't
    allow ``pop`` during ``items()`` traversal.
    """
    expired = [tid for tid, (_, exp) in _cache.items() if exp <= now]
    for tid in expired:
        _cache.pop(tid, None)


def reset_cache_for_testing() -> None:
    """Clear the cache. Test-only — never call from production code."""
    _cache.clear()


async def is_tenant_suppressed(tenant_id: str) -> bool:
    """Cached check: is this tenant currently soft-deleted?

    Returns ``True`` only on a confirmed-suppressed answer. Any other
    case (live, unknown, transport failure) returns ``False`` — the
    boundary fails OPEN so a storage flap can't take core-api down.
    """
    now = time.monotonic()
    cached = _cache.get(tenant_id)
    if cached is not None and cached[1] > now:
        return cached[0]

    try:
        suppressed = await get_storage_client().is_tenant_suppressed(tenant_id)
    except Exception:
        # Fail open. Log but DO NOT block the request — see module
        # docstring for the trade-off.
        #
        # Cache the ``False`` result with the SHORT error-TTL so we
        # don't hammer storage on every subsequent request during an
        # outage. Bot review round 1 on PR #244 (🟠 High): without
        # the cache write, a brief storage blip became a self-amplifying
        # retry storm across every authenticated request. 5 s lets the
        # cache absorb the load and still self-heal quickly once
        # storage recovers (vs. the 30 s success-TTL).
        logger.warning(
            "tenant suppression check failed; failing open",
            extra={"tenant_id": tenant_id},
            exc_info=True,
        )
        _prune_expired(now)
        _cache[tenant_id] = (False, now + _ERROR_TTL_SECONDS)
        return False

    _prune_expired(now)
    _cache[tenant_id] = (suppressed, now + _TTL_SECONDS)
    return suppressed
