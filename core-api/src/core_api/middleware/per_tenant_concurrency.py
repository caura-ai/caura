"""Per-tenant in-flight concurrency caps — two-layer bulkhead.

The slowapi-based ``rate_limit`` middleware bounds request *rate*. A
single tenant burst can still occupy every worker slot in a container
and starve other tenants — exactly the noisy-neighbor pattern the
loadtest harness flagged.

Two caps compose:

1. **Route-entry slot** (``"write"`` / ``"search"``, PR #33). Held for
   the whole request lifecycle — embed + enrich + storage. Acquire
   timeout is short (``per_tenant_acquire_timeout_seconds`` ≈ 50ms);
   fails fast with 429 so a saturated tenant can't queue forever.
   Bounds end-to-end concurrency.

2. **Storage-call slot** (``"storage_write"`` / ``"storage_search"``,
   CAURA-602 follow-up). Held only across the storage roundtrip itself.
   Acquire is unbounded — the outer request budget already caps how
   long the wait can run, and queueing here is the *intended* shape
   (a tenant in the embed phase doesn't hold a storage connection).
   Bounds storage-pool occupancy per tenant — keeps a hot tenant from
   parking every storage-writer connection on a 20-item bulk while
   tenant B waits to write a single row.

State is per-process. With cap ``N`` and Cloud Run ``max_instances``
``M``, the fleet-wide cap is ``N * M`` — size in concert with
``containerConcurrency`` and the storage-writer pool size.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import HTTPException

from core_api.config import settings

logger = logging.getLogger(__name__)

Scope = Literal["write", "search", "storage_write", "storage_search"]

# Per-(scope, tenant) state. Read-modify-write of ``_TENANT_SEMAPHORES``
# in ``_get_semaphore`` is safe without a lock because asyncio runs one
# coroutine at a time on a single event loop and neither ``dict.get``
# nor ``dict.__setitem__`` await — no other task can interleave between
# the miss and the install.
_TENANT_SEMAPHORES: dict[tuple[Scope, str], asyncio.Semaphore] = {}


def _cap_for(scope: Scope) -> int:
    """Resolve the configured cap for ``scope`` from ``Settings``.

    Centralised so callers can't drift — the cap is baked into the
    Semaphore at creation time, so a per-call ``cap`` argument would
    silently bind to the first call's value and ignore subsequent
    setting changes.
    """
    if scope == "write":
        return settings.per_tenant_write_concurrency
    if scope == "search":
        return settings.per_tenant_search_concurrency
    if scope == "storage_write":
        return settings.per_tenant_storage_write_concurrency
    if scope == "storage_search":
        return settings.per_tenant_storage_search_concurrency
    # ``Scope`` already enumerates every valid literal; this is a
    # belt-and-suspenders guard for a future scope added to the type
    # without updating the cap resolver. Without it, ``_get_semaphore``
    # would silently install a Semaphore at the wrong cap and the
    # mistake would only surface as anomalous 429 / queue behaviour
    # under load.
    raise ValueError(f"Unknown concurrency scope: {scope!r}")


def _get_semaphore(scope: Scope, tenant_id: str) -> asyncio.Semaphore:
    """Return (and lazily create) the semaphore for ``(scope, tenant_id)``.

    The dict grows monotonically with the set of tenants the instance
    has seen — bounded by the active tenant count, never reset within
    a process lifetime.
    """
    key = (scope, tenant_id)
    sem = _TENANT_SEMAPHORES.get(key)
    if sem is None:
        sem = asyncio.Semaphore(_cap_for(scope))
        _TENANT_SEMAPHORES[key] = sem
    return sem


@asynccontextmanager
async def per_tenant_slot(
    scope: Literal["write", "search"],
    tenant_id: str,
) -> AsyncIterator[None]:
    """Acquire one of the per-tenant slots for ``scope``, or raise 429
    fast.

    Cap is read from ``Settings`` (``per_tenant_write_concurrency`` /
    ``per_tenant_search_concurrency``) at semaphore-creation time.
    Use as a context manager around the request handler body. Releases
    on exit (success or exception) so a handler raising 5xx still
    frees the slot.

    The ``scope`` parameter is narrowed to the route-entry literals
    only — the deeper ``"storage_*"`` scopes have different semantics
    (unbounded queue) and live behind :func:`per_tenant_storage_slot`.
    Passing a storage scope here would create a fast-fail 429
    semaphore against the same ``(scope, tenant_id)`` key that the
    storage variant uses, with no warning; the type-checker rejects
    that mistake instead.
    """
    sem = _get_semaphore(scope, tenant_id)
    try:
        async with asyncio.timeout(settings.per_tenant_acquire_timeout_seconds):
            await sem.acquire()
    except TimeoutError:
        logger.info(
            "per-tenant concurrency cap reached",
            extra={"scope": scope, "tenant_id": tenant_id, "cap": _cap_for(scope)},
        )
        raise HTTPException(
            status_code=429,
            detail=f"Too many concurrent {scope} requests; retry shortly.",
        )
    try:
        yield
    finally:
        sem.release()


@asynccontextmanager
async def per_tenant_storage_slot(
    scope: Literal["storage_write", "storage_search"],
    tenant_id: str,
) -> AsyncIterator[None]:
    """Acquire a per-tenant slot scoped to the storage roundtrip itself.

    Caller wraps a single ``sc.<call>`` invocation; the slot is held
    only while the storage call is in flight, freeing as soon as the
    response (or its cancellation) returns. Acquisition queues
    unboundedly — the outer request budget (``RequestTimeoutMiddleware``
    or the bulk route's ``asyncio.wait_for``) already caps total wall
    time, and the request slot was already approved at route entry, so
    a second fast-fail here would surface as a confusing 429-after-200.

    Tenant-A storm scenario: A's writes occupy ``cap`` storage slots;
    A's request 5+ queues on ``sem.acquire()``. Tenant B's write enters
    on a different ``(scope, tenant_id)`` key, gets its own semaphore
    with full capacity, and proceeds at baseline latency. The
    storage-writer connection pool stays multi-tenant.
    """
    sem = _get_semaphore(scope, tenant_id)
    # Pre-acquire saturation log: queueing is the *intended* shape (the
    # outer request budget caps wait time), but with no signal here a
    # tenant whose storage writes are backing up generates zero
    # observable evidence — operators just see elevated end-to-end
    # latency with no log to localise the source. DEBUG-level so it
    # doesn't drown low-traffic environments under steady-state load
    # but stays grep-able when investigating a latency spike. Pair with
    # the route-entry slot's INFO-level "cap reached" log (which fires
    # on the fast-fail 429 path) for full observability across both
    # layers.
    if sem.locked():
        logger.debug(
            "per-tenant storage slot saturated; queuing",
            extra={"scope": scope, "tenant_id": tenant_id, "cap": _cap_for(scope)},
        )
    await sem.acquire()
    try:
        yield
    finally:
        sem.release()


def _reset_for_tests() -> None:
    """Drop all tracked semaphores. Test-only — production never calls
    this."""
    _TENANT_SEMAPHORES.clear()
