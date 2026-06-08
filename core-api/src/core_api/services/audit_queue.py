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

logger = logging.getLogger(__name__)


class AuditEventQueue:
    """Bounded in-memory queue for audit events with a background flusher.

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
    ) -> None:
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
        self._flusher_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._dropped_count = 0
        self._flushed_count = 0
        self._failed_count = 0
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
        """Events drained from the queue but lost when the flush callable
        raised. Distinct from ``dropped_count`` (full-queue rejection)
        so an operator can distinguish "writers outpacing the flusher"
        from "storage-api failing the write" in dashboards."""
        return self._failed_count

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    def enqueue(self, event: dict) -> None:
        """Non-blocking enqueue. Drops + warns when the queue is full.

        Synchronous (no ``await``) so callers in fire-and-forget paths
        don't pay an extra event-loop hop. ``Queue.put_nowait`` returns
        immediately when there's room; raises ``QueueFull`` otherwise,
        which we map to a counter + structured warning rather than
        propagating to the caller (audit isn't on the correctness
        path; dropping is preferable to blocking the request).
        """
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
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
            return
        # Wake the flusher early when we reach the batch threshold so a
        # storm doesn't have to wait for the interval timer.
        if self._queue.qsize() >= self._flush_threshold:
            self._wake_event.set()

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

    async def _flusher_loop(self) -> None:
        """Wake on threshold OR interval, drain available events, flush."""
        while not self._stopping.is_set():
            try:
                # Wait for either the threshold-wake event or the
                # interval timer, whichever fires first. ``wait_for``
                # propagates ``TimeoutError`` on the interval path; we
                # catch it and treat as a normal "interval flush" tick.
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=self._flush_interval)
                except TimeoutError:
                    pass
                self._wake_event.clear()
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

        # Final drain on shutdown — best-effort, bounded by stop()'s
        # outer timeout.
        try:
            await self._drain_and_flush()
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

        Failed chunks are NOT requeued — the closure's failure log
        carries enough detail for an operator to correlate the loss
        with the upstream cause; requeuing would risk an infinite
        retry loop on a sustained outage.

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
        events: list[dict] = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if not events:
            return
        for i in range(0, len(events), self._chunk_size):
            chunk = events[i : i + self._chunk_size]
            try:
                await self._flush_callable(chunk)
                self._flushed_count += len(chunk)
                # CAURA-631: callables that drop sentinel events (e.g. audit
                # events without ``tenant_id``) stash the dropped count on
                # themselves so we don't silently credit them as flushed.
                # Defaults to 0 for callables that don't use this protocol.
                self._flushed_count -= getattr(self._flush_callable, "_sentinel_count", 0)
            except BaseException as exc:
                # CAURA-631: when the callable handles per-tenant flushes
                # and only some sub-groups fail, it sets ``failed_event_count``
                # on the raised exception so we credit the actually-failed
                # events to ``_failed_count`` and the rest to
                # ``_flushed_count``. Falls back to ``len(chunk)`` for
                # legacy callables that don't carry the attribute, matching
                # the original whole-chunk-failed accounting.
                #
                # ``BaseException`` (not ``Exception``) so the accounting
                # block runs for ``CancelledError`` / ``SystemExit`` /
                # ``BaseExceptionGroup`` paths too — the per-tenant flusher
                # explicitly attaches ``failed_event_count`` on those raises
                # and we'd otherwise bypass them entirely. We re-raise after
                # accounting so propagation semantics are preserved (the
                # outer flusher loop's ``except Exception`` then handles
                # regular errors and lets BaseExceptions escape unchanged).
                if isinstance(exc, Exception):
                    failed = getattr(exc, "failed_event_count", len(chunk))
                else:
                    # External CancelledError / SystemExit injected at the
                    # ``await self._flush_callable(chunk)`` suspension point
                    # has no ``failed_event_count`` attribute — the per-
                    # tenant flusher's raises always do, but a cancellation
                    # delivered before the flusher returns doesn't. Default
                    # to 0 instead of ``len(chunk)`` so a clean shutdown
                    # doesn't produce a spurious failure-spike on the
                    # dashboard. The events themselves stay in the queue
                    # (or are silently lost depending on shutdown ordering),
                    # but their disposition is "unknown", not "failed".
                    failed = getattr(exc, "failed_event_count", 0)
                self._failed_count += failed
                self._flushed_count += len(chunk) - failed
                # Same sentinel correction as the success path: dropped
                # sentinel events were neither flushed nor failed, so back
                # them out of the surviving-events credit.
                self._flushed_count -= getattr(self._flush_callable, "_sentinel_count", 0)
                if isinstance(exc, Exception):
                    # Per-chunk failure logged here (in addition to the
                    # closure's rich-detail log) so a partial drain of N
                    # chunks shows N independent log lines, each naming
                    # the chunk size — a bare ``logger.exception`` from
                    # the loop's outer ``except`` would only fire once
                    # for the first failing chunk and obscure how many
                    # chunks total were lost. Per-chunk failure isolation
                    # contract: regular ``Exception`` is caught here and
                    # the loop continues to the next chunk (covered by
                    # ``test_chunk_failure_does_not_drop_other_chunks``).
                    logger.exception(
                        "audit chunk flush failed (chunk_size=%d, failed_events=%d); events lost",
                        len(chunk),
                        failed,
                    )
                else:
                    # ``CancelledError`` / ``SystemExit`` /
                    # ``BaseExceptionGroup`` etc — re-raise so shutdown
                    # signals propagate. Per-chunk isolation only applies
                    # to regular ``Exception``; a cancellation pre-empts
                    # the whole drain loop intentionally.
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
