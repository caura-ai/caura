"""Per-tenant in-flight cap on the worker's storage PATCH-back calls.

The worker consumes ``embed-requested`` and ``enrich-requested`` events
in batches sized by ``EVENT_BUS_PUBSUB_MAX_MESSAGES`` (default 25). With
no per-tenant gate, a tenant-A storm fans out into 2 PATCHes per write
(embed + enrich), all hammering the storage-writer pool while tenant B's
single PATCH queues behind. That's the 12.7x ``noisy-neighbor-write``
regression CAURA-636 ran loadtest 1777538050 against — Option 2 (drop
``max_messages`` to 10) confirmed in 1777548665 that aggregate throttling
is the wrong knob; the issue is per-tenant burst.

Mirrors :mod:`core_api.middleware.per_tenant_concurrency`'s
``per_tenant_storage_slot``. State is per-process — Cloud Run worker
containers don't share semaphores. With cap ``N`` and worker
``max_instances`` ``M``, the fleet-wide storage-writer occupancy from a
single tenant is bounded by ``N * M``; size in concert with
``per_tenant_storage_write_concurrency`` on core-api so a single tenant
can't exceed the storage-writer pool from worker + core-api combined.

The worker has no route-entry slot (it accepts no HTTP requests), so
only the queue-unbounded ``storage_slot`` variant is needed. Acquisition
queues — there's no fast-fail 429 path here; the upstream Pub/Sub redelivery
budget already caps how long a single message can stay in flight.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from core_worker.config import settings

logger = logging.getLogger(__name__)

# Bound the registry so a long-lived container with high tenant churn
# doesn't grow ``_TENANT_SEMAPHORES`` without limit. 4096 is comfortably
# above any realistic concurrent-active tenant count per container; cold
# tenants get evicted LRU-style. An evicted tenant whose semaphore is
# still held by in-flight coroutines remains correct — those coroutines
# still hold the only references they need; only the dict entry drops.
# Re-acquire after eviction allocates a fresh semaphore (cap-fresh, no
# inherited backpressure), which is the expected behaviour for a tenant
# that hasn't been seen in a while.
_MAX_TRACKED_TENANTS = 4096

# Read-modify-write of ``_TENANT_SEMAPHORES`` in ``_get_semaphore`` is
# safe without a lock because asyncio runs one coroutine at a time on
# a single event loop and neither ``dict.get`` nor ``dict.__setitem__``
# await — no other task can interleave between the miss and the install.
_TENANT_SEMAPHORES: OrderedDict[str, asyncio.Semaphore] = OrderedDict()


def _get_semaphore(tenant_id: str) -> asyncio.Semaphore:
    """Return (and lazily create) the per-tenant semaphore.

    Cap is read from ``settings`` at semaphore-creation time only;
    ``asyncio.Semaphore`` doesn't support resizing, so a config change
    only applies to tenants seen for the first time after a restart.
    A per-call read would mislead operators into thinking a hot config
    bump rolls out without one.

    Maintains LRU order: a hit moves the entry to the most-recently-used
    end; a miss installs at the MRU end and evicts the LRU entry if the
    registry is full.
    """
    sem = _TENANT_SEMAPHORES.get(tenant_id)
    if sem is None:
        sem = asyncio.Semaphore(settings.per_tenant_storage_write_concurrency)
        if len(_TENANT_SEMAPHORES) >= _MAX_TRACKED_TENANTS:
            _TENANT_SEMAPHORES.popitem(last=False)
        _TENANT_SEMAPHORES[tenant_id] = sem
    else:
        _TENANT_SEMAPHORES.move_to_end(tenant_id)
    return sem


@asynccontextmanager
async def per_tenant_storage_slot(tenant_id: str) -> AsyncIterator[None]:
    """Acquire one of the per-tenant storage slots, queueing if all
    are taken.

    Wrap a single storage PATCH call. Holds the slot only across the
    PATCH roundtrip itself so the embed/enrich provider call (the
    expensive part) doesn't block another tenant's storage write.
    """
    sem = _get_semaphore(tenant_id)
    # ``locked()`` → ``acquire()`` is race-free in single-threaded
    # asyncio: there's no ``await`` between them, so no other coroutine
    # can interleave to release the semaphore before our acquire blocks.
    # The check exists only to localise the saturated-log path; a fully
    # raced state would look the same after acquire and would be logged
    # too late to matter.
    if sem.locked():
        # DEBUG-level: queueing is the *intended* shape — Pub/Sub
        # redelivery already bounds wait time, and an INFO/WARN every
        # time would drown logs under sustained per-tenant load. Stays
        # grep-able when localising a worker latency spike to a
        # specific tenant.
        logger.debug(
            "worker per-tenant storage slot saturated; queuing",
            extra={
                "tenant_id": tenant_id,
                # ``configured_cap`` rather than ``cap``: the semaphore's
                # internal capacity was frozen at creation time, but
                # ``Settings`` isn't ``frozen=True`` so the live setting
                # can drift. The log reflects the current config, not the
                # actual permits ``sem`` will hand out.
                "configured_cap": settings.per_tenant_storage_write_concurrency,
            },
        )
    await sem.acquire()
    try:
        yield
    finally:
        sem.release()


def _reset_for_tests() -> None:
    """Drop tracked semaphores. Test-only — production never calls this."""
    import os

    # Hard guard: a stray production call would silently drop in-flight
    # semaphore state for every tenant the worker has seen, breaking the
    # noisy-neighbor invariant until each tenant's next acquire allocates
    # a fresh sem. ``PYTEST_CURRENT_TEST`` is set by pytest for the
    # duration of every test; absent in any other context.
    assert os.environ.get("PYTEST_CURRENT_TEST"), "_reset_for_tests must only be called from tests"
    _TENANT_SEMAPHORES.clear()
