"""In-process capability-usage aggregator + periodic flush.

Answers "which product capabilities get used, by access path, per org" —
the data behind the adoption report. Both transports (the MCP tool
wrapper in ``tools/_builders.py`` and the REST ``RequestObservationMiddleware``)
call :func:`record` once per logical operation. Records are aggregated in
memory into per-``(tenant, capability, op, transport, minute-bucket)``
counters and flushed to the ``capability_usage`` table on a short
interval, so the request hot path pays only a dict update — never a DB
write.

Why aggregate-then-flush rather than the audit queue's event-per-row
model: usage is a counter, not an event log. Collapsing thousands of
requests into one row per bucket keeps the table small and the flush
cheap. Multiple core-api instances each append their own rows; consumers
SUM at query time (no unique constraint, no upsert → no cross-instance
contention).

Loss model: counts buffered since the last flush are lost if the process
dies mid-interval. Acceptable for adoption analytics (approximate is
fine), and bounded to ``flush_interval`` seconds; ``stop()`` does a final
flush on graceful shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Tenant values that are not real orgs — never attribute usage to them.
_NON_TENANT = frozenset({"", "__unauthenticated__", "__admin__", "__no_auth__"})

# key = (tenant_id, capability, op_or_empty, transport, ts_bucket_iso)
_Key = tuple[str, str, str, str, str]


class _Counter:
    __slots__ = ("count", "duration_ms_sum", "error_count")

    def __init__(self) -> None:
        self.count = 0
        self.error_count = 0
        self.duration_ms_sum = 0


class CapabilityUsageAggregator:
    """In-memory counters with a background interval flusher.

    Lifecycle: ``start()`` from FastAPI lifespan startup,
    ``stop(timeout=...)`` from shutdown. Both idempotent.
    """

    def __init__(
        self,
        *,
        flush_interval_seconds: float,
        flush_callable,  # async (rows: list[dict]) -> None
    ) -> None:
        self._flush_interval = flush_interval_seconds
        self._flush_callable = flush_callable
        self._buckets: dict[_Key, _Counter] = {}
        self._flusher_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._dropped_count = 0

    @staticmethod
    def _bucket(now: datetime) -> datetime:
        # Minute-truncated UTC. Rows in the same minute across flushes and
        # instances share a ts_bucket so they SUM cleanly at query time.
        return now.astimezone(UTC).replace(second=0, microsecond=0)

    def record(
        self,
        *,
        capability: str,
        transport: str,
        tenant_id: str | None,
        op: str | None = None,
        status: str = "ok",
        duration_ms: float = 0.0,
    ) -> None:
        """Synchronous, non-blocking. Folds one operation into its bucket.

        Skips operations with no attributable org (unauthenticated, admin,
        sentinel tenants) — adoption is per-customer and those would
        pollute the org dimension.
        """
        if not tenant_id or tenant_id in _NON_TENANT:
            self._dropped_count += 1
            return
        key: _Key = (
            tenant_id,
            capability,
            op or "",
            transport,
            self._bucket(datetime.now(UTC)).isoformat(),
        )
        c = self._buckets.get(key)
        if c is None:
            c = _Counter()
            self._buckets[key] = c
        c.count += 1
        if status != "ok":
            c.error_count += 1
        # Negative/NaN guard: only fold sane durations into the sum.
        if duration_ms and duration_ms > 0:
            c.duration_ms_sum += int(duration_ms)

    def _drain_rows(self) -> list[dict]:
        """Atomically swap out the current buckets and render them as rows.

        Swap is a single statement with no ``await`` in between, so it's
        atomic against concurrent ``record()`` calls on the event loop.
        """
        pending = self._buckets
        self._buckets = {}
        rows: list[dict] = []
        for (tenant_id, capability, op, transport, ts_iso), c in pending.items():
            rows.append(
                {
                    "tenant_id": tenant_id,
                    "capability": capability,
                    "op": op or None,
                    "transport": transport,
                    "ts_bucket": datetime.fromisoformat(ts_iso),
                    "count": c.count,
                    "error_count": c.error_count,
                    "duration_ms_sum": c.duration_ms_sum,
                }
            )
        return rows

    async def start(self) -> None:
        if self._flusher_task is not None and not self._flusher_task.done():
            return
        self._stopping.clear()
        self._flusher_task = asyncio.create_task(self._flusher_loop(), name="capability-usage-flusher")

    async def stop(self, *, timeout: float = 5.0) -> None:
        if self._flusher_task is None:
            return
        self._stopping.set()
        try:
            await asyncio.wait_for(self._flusher_task, timeout=timeout)
        except TimeoutError:
            logger.warning(
                "capability-usage flusher did not drain within %ss; cancelling",
                timeout,
            )
            self._flusher_task.cancel()
            try:
                await self._flusher_task
            except (asyncio.CancelledError, Exception):
                pass
        finally:
            self._flusher_task = None

    async def _flusher_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=self._flush_interval)
                except TimeoutError:
                    pass  # normal interval tick
                await self._flush_once()
            except Exception:
                # A flush failure must not kill the loop — the alternative
                # is silently stopping adoption capture for the process
                # lifetime. Counts in the failed batch are lost (see module
                # docstring loss model).
                logger.exception("capability-usage flush failed; continuing")
        # Final drain on shutdown — best-effort, bounded by stop()'s timeout.
        try:
            await self._flush_once()
        except Exception:
            logger.exception("capability-usage shutdown flush failed")

    async def _flush_once(self) -> None:
        rows = self._drain_rows()
        if not rows:
            return
        await self._flush_callable(rows)


async def _default_flush(rows: list[dict]) -> None:
    """Append rows to ``capability_usage`` via a plain (RLS-free) session.

    Cross-tenant by design — the flush carries many tenants' counters in
    one batch, so it runs WITHOUT an ``app.tenant_id`` context. The table
    has no RLS policy, so the insert is unrestricted (see the migration).
    """
    from common.models.capability_usage import CapabilityUsage
    from core_api.db.session import async_session

    async with async_session() as session:
        session.add_all([CapabilityUsage(**r) for r in rows])
        await session.commit()


# Module-level singleton bound at lifespan startup. None → recording is a
# no-op (early startup, tests that don't wire it, or feature disabled).
_aggregator_singleton: CapabilityUsageAggregator | None = None


def get_aggregator() -> CapabilityUsageAggregator | None:
    return _aggregator_singleton


def set_aggregator(agg: CapabilityUsageAggregator | None) -> None:
    global _aggregator_singleton
    _aggregator_singleton = agg


def record_usage(
    *,
    capability: str,
    transport: str,
    tenant_id: str | None,
    op: str | None = None,
    status: str = "ok",
    duration_ms: float = 0.0,
) -> None:
    """Module-level convenience: record to the active aggregator if any.

    Safe to call from anywhere — a no-op when the aggregator isn't wired
    (so emitters never need to null-check). Never raises into the caller's
    hot path.
    """
    agg = _aggregator_singleton
    if agg is None:
        return
    try:
        agg.record(
            capability=capability,
            transport=transport,
            tenant_id=tenant_id,
            op=op,
            status=status,
            duration_ms=duration_ms,
        )
    except Exception:
        # Adoption capture must never break a request.
        logger.exception("capability-usage record failed; ignoring")
