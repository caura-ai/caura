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

from common.http_retry import CONNECT_PHASE_EXCEPTIONS
from core_api.clients.storage_client import get_storage_client

logger = logging.getLogger(__name__)

#: Per-flush ceiling on individually-named dropped rows (OSS 09/02 L-44).
_DROPPED_ROW_LOG_LIMIT = 20

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
        #: Counter rows dropped rather than replayed after an ambiguous or
        #: cancelled flush. The audit path got ``interrupted_count`` in this
        #: same change; the billing path is where the number matters more.
        self.dropped_rows = 0
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

    async def _rebuffer(self, batch: dict) -> None:
        """Merge a batch back into the buffer, with whatever arrived meanwhile."""
        async with self._lock:
            for key, count in batch.items():
                self._counts[key] += count

    def _log_dropped(self, rows: list[dict], reason: str) -> None:
        """Name dropped counter rows so the shortfall is recoverable by hand.

        Capped. ``_counts`` is keyed by ``(tenant_id, operation, period)`` and
        ``tenant_id`` is unbounded, so a busy multi-tenant instance can carry
        hundreds of rows per flush — and a sustained storage incident would
        repeat that every ``FLUSH_INTERVAL_SECONDS``. ``usage_service`` already
        refuses the uncapped version of this in the same subsystem
        (``_METER_FAILURE_LOG_EVERY``: "one traceback per failed write would
        turn a meter outage into a log-volume incident on top of a metering
        one"). A bounded sample plus a total is what a hand recovery needs;
        ``dropped_rows`` is what tells an operator how big the leak is, which a
        grep over ERROR lines does not.
        """
        self.dropped_rows += len(rows)
        for row in rows[:_DROPPED_ROW_LOG_LIMIT]:
            logger.error(
                "usage meter counter row dropped (%s): tenant=%s operation=%s period_start=%s count=%d",
                reason,
                row["tenant_id"],
                row["operation"],
                row["period_start"],
                row["count"],
            )
        if len(rows) > _DROPPED_ROW_LOG_LIMIT:
            logger.error(
                "usage meter: %d further counter rows dropped (%s), not listed; %d rows dropped since start",
                len(rows) - _DROPPED_ROW_LOG_LIMIT,
                reason,
                self.dropped_rows,
            )

    async def flush(self) -> int:
        """Write and clear the buffer. Returns the number of rows sent.

        OSS 09/02 L-44 — a batch is returned to the buffer ONLY when the
        request provably never reached storage. ``/tenant-usage/increment`` is
        an additive upsert with no storage-side dedupe, so replaying a write
        that did commit bills the tenant twice. ``common/http_retry`` already
        draws this exact line for non-idempotent POSTs — connection-phase
        failures are raised before a byte is written, while "ReadTimeout and
        5xx are NOT retried there — the request reached storage and may have
        committed" — and ``increment_tenant_usage`` goes out at the
        ``idempotent=False`` default accordingly. This method used to replay on
        ANY failure one layer up, which is the thing that policy refuses.

        Under-counting is the deliberate direction: charging a tenant for work
        they did not do is the worse error and the one nobody outside can
        detect. Dropped rows are logged individually. The durable fix is an
        idempotency key on that endpoint, which is what would make a whole
        batch safe to replay.
        """
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
        try:
            return await get_storage_client().increment_tenant_usage(rows)
        except CONNECT_PHASE_EXCEPTIONS:
            # Raised before a single request byte is written, so re-sending
            # cannot double-bill. A blip here costs latency, not accuracy.
            logger.exception(
                "usage meter flush failed before transmission; %d counter rows returned to the buffer",
                len(rows),
            )
            await self._rebuffer(batch)
            return 0
        except Exception:
            # AMBIGUOUS — ReadTimeout, 5xx, a response lost after the commit.
            logger.exception(
                "usage meter flush failed ambiguously; %d counter rows dropped "
                "rather than risk double-billing a committed write",
                len(rows),
            )
            self._log_dropped(rows, "ambiguous flush failure")
            return 0
        except BaseException:
            # ``CancelledError`` from ``stop()``, and NOT re-buffered — which
            # is the opposite of what it looks like it should do.
            #
            # ``CoreStorageClient._cancel_safe`` wraps every request in
            # ``asyncio.shield`` on purpose: cancelling mid-request used to
            # strand the pooled connection (incident 2026-06-16), so the
            # request now "runs to completion while the caller still observes
            # CancelledError immediately". The POST therefore very likely
            # COMMITS after this frame has already unwound. Putting the batch
            # back would hand it to ``stop()``'s final flush and bill it twice
            # — the precise failure L-44 is about, on the one path that looked
            # safe to exempt.
            #
            # Dropping instead means a shutdown racing a flush can lose an
            # interval of counts if the shielded write did NOT land. That is
            # the same under-count-rather-than-over-count trade as above.
            logger.warning(
                "usage meter flush cancelled; %d counter rows dropped — the shielded "
                "request may still commit, so replaying them could double-bill",
                len(rows),
            )
            self._log_dropped(rows, "flush cancelled at shutdown")
            raise

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
        can land inside that loop's own in-flight flush, and since OSS 09/02
        L-44 that batch is DROPPED rather than returned to the buffer:
        ``_cancel_safe`` shields the request, so the POST very likely commits
        after the frame has unwound, and handing it to the flush below would
        bill it twice. ``flush`` logs those rows and counts them in
        ``dropped_rows``.

        Only a connection-phase failure — provably never transmitted — comes
        back to the buffer for that final flush to re-send. An ambiguous one
        (ReadTimeout, 5xx — the request may have committed) drops its batch
        too, rather than replay an additive upsert that has no storage-side
        dedupe. So a shutdown racing a storage blip can still lose counts;
        what it can no longer do is bill them twice.

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
        dropped_before = self.dropped_rows
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
            # ``flush`` moved the buffer into its own batch before the deadline
            # cancelled it, and since OSS 09/02 L-44 that batch is DROPPED, not
            # put back. So ``_counts`` holds only what raced in DURING the
            # flush: reading its length here would report ~0 rows lost on the
            # one path where the loss is largest. Both numbers are gone once
            # this returns — the dropped batch is already named row-by-row by
            # ``_log_dropped``; what is still buffered has nobody left to flush
            # it.
            logger.warning(
                "usage meter final flush did not complete within %ss; %d counter rows dropped "
                "by the cancelled flush and %d still buffered are lost to shutdown",
                timeout,
                self.dropped_rows - dropped_before,
                len(self._counts),
            )
