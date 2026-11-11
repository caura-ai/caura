"""In-memory audit-event queue + background batched flusher (CAURA-628).

Pre-CAURA-628 every memory write triggered a fire-and-forget background
task that POSTed one audit event to ``core-storage-api``. Under bulk
storms (100-item batches x tenant A's 20-concurrent storm = up to 2000
audit POSTs in flight) those calls saturated the storage-api per-tenant
slot AND piled up on the AlloyDB ``audit_log`` table-level write lock,
queueing tenant B's gentle audit traffic behind tenant A's fan-out.
That cross-tenant queueing is the residual contention the CAURA-627
deep-dive identified as the dominant remaining ``noisy-neighbor-write``
cause, after the LLM-pool fix in #34 and the dead-index drop in #35.

This module replaces the per-event POST with an in-memory ``asyncio``
queue + a background flusher that batches events and writes them to a
new ``POST /audit-logs/bulk`` endpoint. Flush triggers are first-of:

  - ``audit_queue_flush_threshold`` events accumulated (50 by default)
  - ``audit_queue_flush_interval_seconds`` elapsed since last flush (1s)

Per-event memory overhead is small (~200 bytes for an audit dict at the
default sizes), so a 10000-event cap fits in ~2 MiB per worker process —
plenty of headroom over the realistic worst-case storm rate.

Backpressure: the queue is bounded. If writers fill the queue faster
than the flusher drains it (storage-api degraded, or a sustained burst
larger than the cap), new events are dropped with a structured warning
counter rather than blocking the request hot path. Audit is not on the
critical correctness path; loss of a few events under sustained
overload is preferable to defeating the whole point of async ingestion
by blocking writers.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from time import monotonic

logger = logging.getLogger(__name__)


@dataclass
class _RetryChunk:
    events: list[dict]
    attempts: int
    retry_at: float


class AuditEventQueue:
    """Bounded in-memory queue for audit events with a background flusher.

    Within this queue, later events for a tenant wait behind its retained
    failures until recovery or final loss. Independent tenants keep moving.
    This does not order writes across processes or synchronous fallback calls.

    Lifecycle: ``start()`` from FastAPI lifespan startup,
    ``stop(timeout=...)`` from shutdown. Both are idempotent so a
    re-import in tests doesn't double-start the task.
    """

    def __init__(
        self,
        *,
        max_queue_size: int,
        flush_threshold: int,
        flush_interval_seconds: float,
        flush_callable,  # async (events: list[dict]) -> None
        chunk_size: int = 500,
        max_flush_attempts: int = 3,
        retry_initial_backoff_seconds: float = 1.0,
        retry_max_backoff_seconds: float = 30.0,
    ) -> None:
        if max_flush_attempts < 1:
            raise ValueError("max_flush_attempts must be positive")
        if retry_initial_backoff_seconds < 0 or retry_max_backoff_seconds < 0:
            raise ValueError("retry backoff must be nonnegative")
        self._max_queue_size = max_queue_size
        self._flush_threshold = flush_threshold
        self._flush_interval = flush_interval_seconds
        self._flush_callable = flush_callable
        # ``chunk_size`` caps the per-call batch given to ``flush_callable``.
        # The storage-side ``POST /audit-logs/bulk`` endpoint enforces
        # ``_MAX_BATCH_SIZE = 500``; if the queue ever holds more than
        # that (e.g. storage-api was degraded and the queue built up),
        # a single uncapped flush would 422 and lose every drained
        # event. Slicing the drained list into chunks keeps each flush
        # call within the storage cap, and per-chunk failure isolation
        # means a bad chunk doesn't take down the rest of the drain.
        # Default matches the storage-side cap exactly; tests pass
        # ``chunk_size=1`` to exercise the slicing path without
        # depending on the storage-side constant.
        self._chunk_size = chunk_size
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=max_queue_size)
        # Retried slices are the logical front of the queue. Keeping them
        # separate preserves their attempt/deadline state without changing
        # payloads or using asyncio.Queue's private internals. Both buffers
        # share max_queue_size; a drain holds at most one more bounded snapshot.
        self._retry_chunks: deque[_RetryChunk] = deque()
        self._retry_event_count = 0
        self._max_flush_attempts = max_flush_attempts
        self._retry_initial_backoff = retry_initial_backoff_seconds
        self._retry_max_backoff = retry_max_backoff_seconds
        self._flusher_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._dropped_count = 0
        self._flushed_count = 0
        self._failed_count = 0
        self._interrupted_count = 0
        self._wake_event = asyncio.Event()  # set when threshold reached

    @property
    def dropped_count(self) -> int:
        """Events rejected at enqueue time because the queue was full."""
        return self._dropped_count

    @property
    def flushed_count(self) -> int:
        """Events successfully written via the flush callable."""
        return self._flushed_count

    @property
    def failed_count(self) -> int:
        """Events lost at the attempt/retention caps, or known to have failed
        during an interruption. Retained retries are not losses. Distinct
        from ``dropped_count`` (full-queue rejection), so an operator can
        distinguish writers outpacing the flusher from failed storage writes.
        """
        return self._failed_count

    @property
    def interrupted_count(self) -> int:
        """Events whose disposition is genuinely UNKNOWN (OSS 08/14 L-11).

        A ``CancelledError`` delivered at the ``_flush_callable`` suspension
        point tells us nothing about whether storage committed the chunk. That
        is not success and not failure, and it used to be credited to
        ``flushed_count`` — the one bucket that overstates success — because
        the alternative considered was a spurious failure-spike. On the AUDIT
        path that reported silent loss as a clean drain.

        Its own counter instead: a shutdown that interrupts a flush now shows
        up as interrupted, leaving ``flushed_count`` meaning only what its
        docstring says.
        """
        return self._interrupted_count

    @property
    def queue_size(self) -> int:
        return self._queue.qsize() + self._retry_event_count

    def enqueue(self, event: dict, *, silent: bool = False) -> bool:
        """Non-blocking enqueue. Drops + warns when the queue is full.

        Returns ``True`` when the event was queued, ``False`` when the
        queue was full and the event dropped. Fire-and-forget callers ignore
        the return — the prior ``None`` was already falsy, so existing call
        sites are unaffected. A compliance-critical caller inspects it to fall
        back to a synchronous write for an event that must not be lost (see
        ``log_action(critical=True)``).

        ``silent``: when True, a full-queue rejection returns ``False``
        without incrementing the drop counter or logging — for a caller that
        recovers the event itself (e.g. ``log_action(critical=True)``, which
        falls back to a synchronous write), so a recovered event isn't
        miscounted as a loss.

        Synchronous (no ``await``) so callers in fire-and-forget paths
        don't pay an extra event-loop hop. ``Queue.put_nowait`` returns
        immediately when there's room; raises ``QueueFull`` otherwise,
        which we map to a counter + structured warning rather than
        propagating to the caller (audit isn't on the correctness
        path; dropping is preferable to blocking the request).
        """
        try:
            if self.queue_size >= self._max_queue_size:
                raise asyncio.QueueFull
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            if silent:
                # Caller will recover this event synchronously — not a real
                # drop, so leave the metric and the warning untouched.
                return False
            self._dropped_count += 1
            # Log every 100th drop so a sustained overload produces a
            # finite, informative log volume rather than one line per
            # dropped event. A single first-drop line still surfaces
            # immediately so the issue is visible.
            if self._dropped_count == 1 or self._dropped_count % 100 == 0:
                logger.warning(
                    "audit queue full; dropped %d events so far "
                    "(max_queue_size=%d, flush behind storage-api?)",
                    self._dropped_count,
                    self._max_queue_size,
                )
            return False
        # Wake the flusher early when we reach the batch threshold so a
        # storm doesn't have to wait for the interval timer. New arrivals also
        # wake a retry wait, so independent tenants needn't wait out backoff.
        if self.queue_size >= self._flush_threshold or self._retry_chunks:
            self._wake_event.set()
        return True

    async def start(self) -> None:
        """Idempotent: a second call is a no-op."""
        if self._flusher_task is not None and not self._flusher_task.done():
            return
        self._stopping.clear()
        self._flusher_task = asyncio.create_task(self._flusher_loop(), name="audit-queue-flusher")

    async def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the flusher to drain pending events and exit.

        Waits up to ``timeout`` seconds for the final flush. If the
        deadline expires (storage-api hung), the flusher task is
        cancelled and any still-in-queue events are lost — the
        alternative is blocking shutdown indefinitely, which would
        block Cloud Run's revision termination and trigger
        ``container did not stop in time`` SIGKILL anyway.
        """
        if self._flusher_task is None:
            return
        self._stopping.set()
        self._wake_event.set()  # break the loop's wait immediately
        try:
            await asyncio.wait_for(self._flusher_task, timeout=timeout)
        except TimeoutError:
            logger.warning(
                "audit queue flusher did not drain within %ss; cancelling",
                timeout,
            )
            self._flusher_task.cancel()
            try:
                await self._flusher_task
            except (asyncio.CancelledError, Exception):
                pass
        finally:
            self._flusher_task = None

    def _next_retry_delay(self) -> float | None:
        """Earliest eligible retry; later same-tenant deferrals are not ready."""
        blocked: set[str | None] = set()
        earliest: float | None = None
        for pending in self._retry_chunks:
            tenants = {event.get("tenant_id") for event in pending.events}
            if tenants - blocked:
                earliest = pending.retry_at if earliest is None else min(earliest, pending.retry_at)
            blocked.update(tenants)
        return None if earliest is None else max(0.0, earliest - monotonic())

    async def _wait_for_flush(self, timeout: float) -> None:
        # New arrivals can wake a retry wait, including below the batching
        # threshold, so an independent tenant never waits out another's backoff.
        # Retained slices remain queued throughout this cancellable wait.
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
        except TimeoutError:
            pass
        self._wake_event.clear()

    async def _flusher_loop(self) -> None:
        """Wake on threshold OR interval, drain available events, flush."""
        while not self._stopping.is_set():
            try:
                retry_delay = self._next_retry_delay()
                timeout = (
                    self._flush_interval if retry_delay is None else min(self._flush_interval, retry_delay)
                )
                await self._wait_for_flush(0 if self._queue.qsize() else timeout)
                await self._drain_and_flush()
            except Exception:
                # ``_drain_and_flush`` catches per-chunk flush failures
                # internally (logs + bumps ``_failed_count``) so this
                # ``except`` only fires on genuinely unexpected errors
                # — queue corruption, an asyncio internal bug, etc. —
                # not on transient storage-api blips. The loop must
                # still survive those, since the alternative is silent
                # audit ingestion stop.
                logger.exception("audit flush iteration failed unexpectedly; continuing")

        # Include retained retries in the final drain, with the same backoff
        # and attempt caps. stop()'s outer timeout still bounds shutdown.
        try:
            while self.queue_size:
                await self._drain_and_flush()
                if self.queue_size:
                    retry_delay = self._next_retry_delay()
                    timeout = self._flush_interval if retry_delay is None else retry_delay
                    await self._wait_for_flush(0 if self._queue.qsize() else timeout)
        except Exception:
            logger.exception("audit shutdown flush failed")

    async def _drain_and_flush(self) -> None:
        """Drain the queue, then flush in ``chunk_size`` slices.

        Slicing protects against two failure modes that a single-call
        flush exposes:

        1. Storage-side hard caps: ``POST /audit-logs/bulk`` rejects
           batches above ``_MAX_BATCH_SIZE`` (500). Without slicing, a
           queue that built up past 500 (storage-api outage,
           tenant-A-storm pile-up) would have its entire drained
           backlog rejected on the first recovery flush.
        2. Per-chunk failure isolation: a bad chunk (one transient
           5xx, one bad event after future relaxations) doesn't take
           down the rest of the drain. Each chunk's success/failure
           is counted independently in ``_flushed_count`` /
           ``_failed_count``.

        Failed chunks return to the logical front for a later pass, with
        exponential backoff and bounded attempts. Retention shares the queue
        cap with new events. Each pass attempts its snapshot only once, so a
        sustained outage cannot cause an unbounded retry loop.

        Once a tenant has a retained failure, its later events are deferred
        behind that failure without spending an attempt. Other tenants may
        still flush. This preserves enqueue order for surviving events within
        this queue, including failures that persist across several drains.
        Future retries stay queued without sleeping in this drain, so due
        retries and fresh events for independent tenants are not held behind
        backoff. The outer loop waits for the next eligible deadline.

        ``task_done()`` is intentionally NOT called here. The
        documented ``asyncio.Queue.join()`` contract is "wait until
        all enqueued items have been processed" — calling
        ``task_done()`` before the flush would let ``join()`` return
        while events were still buffered in ``events`` and not yet
        written to storage. Nothing in the codebase calls
        ``queue.join()`` today; if a future caller wires it for a
        stricter drain semantic, the right place for ``task_done()``
        is after the per-chunk flush completes successfully (or
        explicitly fails). Today's shutdown path uses ``stop()`` +
        the final ``_drain_and_flush`` call, which doesn't depend on
        the unfinished-task counter at all.
        """
        retry_chunks = len(self._retry_chunks)
        new_events = self._queue.qsize()
        blocked_tenants: set[str | None] = set()
        # Scan this pass's retained slices once, rotating future work back in
        # the same order. A skipped slice still blocks later same-tenant events.
        for _ in range(retry_chunks):
            pending = self._retry_chunks.popleft()
            if pending.retry_at > monotonic():
                self._retry_chunks.append(pending)
                blocked_tenants.update(event.get("tenant_id") for event in pending.events)
                continue
            self._retry_event_count -= len(pending.events)
            await self._flush_chunk(
                pending.events,
                attempts=pending.attempts,
                retry_at=pending.retry_at,
                blocked_tenants=blocked_tenants,
            )
        events = [self._queue.get_nowait() for _ in range(new_events)]
        for i in range(0, len(events), self._chunk_size):
            chunk = events[i : i + self._chunk_size]
            await self._flush_chunk(chunk, attempts=0, retry_at=0, blocked_tenants=blocked_tenants)

    def _retain_chunk(self, events: list[dict], *, attempts: int, retry_at: float) -> bool:
        if len(events) > self._max_queue_size - self.queue_size:
            return False
        self._retry_chunks.append(_RetryChunk(events, attempts, retry_at))
        self._retry_event_count += len(events)
        return True

    async def _flush_chunk(
        self, chunk: list[dict], *, attempts: int, retry_at: float, blocked_tenants: set[str | None]
    ) -> None:
        deferred = [event for event in chunk if event.get("tenant_id") in blocked_tenants]
        chunk = [event for event in chunk if event.get("tenant_id") not in blocked_tenants]
        if deferred and not self._retain_chunk(deferred, attempts=attempts, retry_at=retry_at):
            self._failed_count += len(deferred)
            logger.error(
                "audit chunk deferral failed (chunk_size=%d, failed_events=%d); events lost "
                "(retention cap reached)",
                len(deferred),
                len(deferred),
            )
        if not chunk:
            return
        try:
            await self._flush_callable(chunk)
            self._flushed_count += len(chunk)
            # Callables discard unattributable sentinels without writing them.
            self._flushed_count -= getattr(self._flush_callable, "_sentinel_count", 0)
        except BaseException as exc:
            if isinstance(exc, Exception):
                # Production's per-tenant fan-out identifies the failed subset;
                # successful sibling tenants and sentinels are never retried.
                # A generic callable's failure leaves the whole chunk unknown,
                # including an unacknowledged commit: retry with the same ids.
                failed_events = getattr(exc, "failed_events", chunk)
                self._flushed_count += len(chunk) - len(failed_events)
                self._flushed_count -= getattr(self._flush_callable, "_sentinel_count", 0)
                attempts += 1
                delay = min(
                    self._retry_initial_backoff * 2 ** (attempts - 1),
                    self._retry_max_backoff,
                )
                if attempts < self._max_flush_attempts and self._retain_chunk(
                    failed_events, attempts=attempts, retry_at=monotonic() + delay
                ):
                    blocked_tenants.update(event.get("tenant_id") for event in failed_events)
                    logger.warning(
                        "audit chunk flush failed (chunk_size=%d, failed_events=%d); "
                        "retained for retry after %ss (attempt=%d/%d)",
                        len(chunk),
                        len(failed_events),
                        delay,
                        attempts,
                        self._max_flush_attempts,
                    )
                else:
                    self._failed_count += len(failed_events)
                    logger.exception(
                        "audit chunk flush failed (chunk_size=%d, failed_events=%d); events lost "
                        "(attempt or retention cap reached)",
                        len(chunk),
                        len(failed_events),
                    )
                return

            # Preserve interruption accounting and propagation. Cancellation
            # says nothing about whether an in-flight storage POST committed.
            failed = getattr(exc, "failed_event_count", None)
            if failed is None:
                self._interrupted_count += len(chunk)
            else:
                self._failed_count += failed
                self._flushed_count += len(chunk) - failed
                self._flushed_count -= getattr(self._flush_callable, "_sentinel_count", 0)
            raise


# Module-level singleton bound at lifespan startup. Tests that need a
# different instance can monkeypatch this binding directly.
_queue_singleton: AuditEventQueue | None = None


def get_audit_queue() -> AuditEventQueue | None:
    """Return the active queue or ``None`` if not initialised.

    ``None`` means audit ingestion is in synchronous-fallback mode —
    callers in ``log_action`` fall back to the legacy per-event POST
    path. Used during early startup, in tests that don't wire the
    queue, and on the synchronous-fallback path during the dual-write
    rollout window (so a queue-side bug can't drop audit events
    silently).
    """
    return _queue_singleton


def set_audit_queue(queue: AuditEventQueue | None) -> None:
    """Bind / unbind the module-level singleton. Used by lifespan + tests."""
    global _queue_singleton
    _queue_singleton = queue
