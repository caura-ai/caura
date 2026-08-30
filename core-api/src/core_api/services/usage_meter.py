"""The storage-backed implementation behind ``ServiceHooks.usage_meter``.

caura-ai/caura-enterprise#83. ``usage_service`` provides the seam; this is what
gets wired into it, and it is what makes the counters actually move.

WHY THIS LIVES IN OSS AND WRITES TO core-storage-api
-----------------------------------------------------
The counts have to come from core-api — no interceptor upstream of it can see
``len(body.items)`` on a bulk write, the ``if auth.tenant_id:`` admin skip at
every metered call site, or the idempotent replay that deliberately consumes no
quota. So the meter runs here.

It writes through the storage client core-api already holds, rather than
calling the platform. core-api has no route to a platform service in any
environment — its only ``PLATFORM_*`` settings are LLM config, and the
dependency runs the other way (platform-admin-api holds ``CORE_API_URL``).
Introducing core → platform for metering would invert that. The platform reads
``tenant_usage_counters`` instead, through core-storage-api's
``POST /tenant-usage/query``.

CORRECTION: this docstring previously said the platform would read the table
directly, "so that costs it no network hop". They do share a database, but
reading across schemas is not the boundary this codebase keeps — the rationale
is in that router's docstring.

BUFFERED, NOT PER-WRITE
-----------------------
This sits on the write path of every metered route. Two prior decisions in this
codebase point the same way — CAURA-628 moved audit off per-mutation POSTs, and
``capability_usage`` aggregates in memory "so the request hot path pays only a
dict update". So the meter coalesces in a dict and flushes on an interval.

The difference from ``capability_usage``, and the reason this is safe for
billing: the flush is an **additive upsert**, so a crash loses only the counts
buffered since the last flush rather than corrupting a total, and two instances
flushing the same period both land. The buffer is also flushed on shutdown.
Under a short interval the exposure is seconds of counts — the price of not
putting a storage round-trip in front of every write.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import defaultdict
from datetime import UTC, datetime

from core_api.clients.storage_client import get_storage_client

logger = logging.getLogger(__name__)

#: How often buffered counts are written. Short enough that a crash costs
#: seconds, long enough that a busy tenant is one upsert rather than thousands.
FLUSH_INTERVAL_SECONDS = 15


def current_period_start(now: datetime | None = None) -> datetime:
    """Start of the billing period ``now`` falls in — UTC, month-truncated.

    Computed here rather than in the database so a row cannot land in a period
    other than the one the caller counted in, and so a backfill can name a
    closed period explicitly.
    """
    ts = (now or datetime.now(UTC)).astimezone(UTC)
    return ts.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class UsageMeter:
    """Coalesces metered operations and flushes them as additive upserts."""

    def __init__(self, flush_interval: float = FLUSH_INTERVAL_SECONDS) -> None:
        self._counts: dict[tuple[str, str, datetime], int] = defaultdict(int)
        self._flush_interval = flush_interval
        self._task: asyncio.Task | None = None
        # Guards the swap in ``flush``. Increments themselves are plain dict
        # updates under the event loop's single thread, so they need no lock;
        # the swap does, or a concurrent flush could drop the other's batch.
        self._lock = asyncio.Lock()

    async def record(self, *, tenant_id: str, operation: str, count: int = 1):
        """The hook entry point. Buffers and returns ``None``.

        ``None`` rather than counters: reporting ``limit``/``remaining`` would
        mean a read per write, and nothing enforces the verdict here anyway —
        core-api's limit enforcement arrives out-of-band via
        ``x-org-read-only``. See ``usage_service``.
        """
        self._counts[(tenant_id, operation, current_period_start())] += count
        return None

    async def flush(self) -> int:
        """Write and clear the buffer. Returns the number of rows sent."""
        async with self._lock:
            if not self._counts:
                return 0
            batch = self._counts
            self._counts = defaultdict(int)
        rows = [
            {
                "tenant_id": tenant_id,
                "operation": operation,
                "period_start": period.isoformat(),
                "count": count,
            }
            for (tenant_id, operation, period), count in batch.items()
        ]
        sent = False
        try:
            written = await get_storage_client().increment_tenant_usage(rows)
            sent = True
            return written
        except Exception:
            logger.exception(
                "usage meter flush failed; %d counter rows returned to the buffer",
                len(rows),
            )
            return 0
        finally:
            if not sent:
                # Put the counts BACK rather than dropping them: the upsert is
                # additive, so re-sending is safe, and a transient storage blip
                # should cost latency rather than billing accuracy. Merged with
                # whatever arrived while the flush was in flight.
                #
                # In ``finally`` rather than in the ``except``, because the
                # batch is already out of the buffer and NOTHING may leave this
                # frame without putting it back. ``stop()`` cancels this task,
                # and a CancelledError landing on the await above is a
                # BaseException that walks straight past ``except Exception`` —
                # dropping precisely the counts ``stop()``'s final flush exists
                # to save.
                #
                # This ``acquire`` runs while that cancellation is propagating,
                # and completes because it takes the uncontended fast path,
                # which does not yield. That holds because ``flush`` is never
                # re-entered concurrently: its only callers are the loop task
                # and ``stop()``, and ``stop()`` joins the task before its own
                # call. A third caller would have to re-check this.
                async with self._lock:
                    for (tenant_id, operation, period), count in batch.items():
                        self._counts[(tenant_id, operation, period)] += count

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._flush_interval)
            try:
                await self.flush()
            except Exception:  # pragma: no cover - defence, flush self-handles
                logger.exception("usage meter flush loop error")

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self, *, timeout: float = 5.0) -> None:
        """Cancel the loop and flush what is left, within ``timeout``.

        The final flush is the difference between a clean shutdown costing
        nothing and costing up to one interval of counts. Cancelling the loop
        can land inside that loop's own in-flight flush; ``flush`` returns its
        batch to the buffer on every failure path, so this method's cancel
        never drops counts and the flush below re-sends them.

        The deadline is the same trade ``audit_queue`` documents: the storage
        client will wait 120s on a read, which is far longer than Cloud Run
        grants a terminating revision. Blocking here past the grace period
        would lose these counts to SIGKILL *and* take the remaining shutdown
        steps — the event bus and the storage client's own close — with it.
        """
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        try:
            await asyncio.wait_for(self.flush(), timeout=timeout)
            for (tenant_id, operation, period), count in self._counts.items():
                logger.error(
                    "usage meter counter row lost to shutdown: tenant=%s operation=%s period_start=%s count=%d",
                    tenant_id,
                    operation,
                    period.isoformat(),
                    count,
                )
        except TimeoutError:
            logger.warning(
                "usage meter final flush did not complete within %ss; %d counter rows are lost to shutdown",
                timeout,
                len(self._counts),
            )
