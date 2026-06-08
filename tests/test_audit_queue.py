"""Tests for core_api.services.audit_queue (CAURA-628).

Covers the in-memory queue + background flusher behaviours that
``log_action`` relies on:

- enqueue is non-blocking and triggers a flush when the threshold is
  reached
- the interval timer also triggers a flush even if the threshold
  hasn't been hit (steady-state low-volume tenants)
- shutdown drains pending events
- queue overflow drops + warns rather than blocking the request hot
  path
- flush failures don't kill the loop
"""

from __future__ import annotations

import asyncio

import pytest

from core_api.services.audit_queue import AuditEventQueue


@pytest.mark.asyncio
async def test_enqueue_triggers_flush_at_threshold() -> None:
    """When the queue reaches ``flush_threshold``, the flusher wakes
    immediately rather than waiting for the interval timer."""
    flushed_batches: list[list[dict]] = []

    async def flush(events: list[dict]) -> None:
        flushed_batches.append(events)

    q = AuditEventQueue(
        max_queue_size=100,
        flush_threshold=3,
        flush_interval_seconds=10.0,  # well past the threshold trigger
        flush_callable=flush,
    )
    await q.start()
    try:
        for i in range(3):
            q.enqueue({"i": i})
        # Yield enough cycles for the threshold-wake → drain → flush
        # callable to run. Two await sleeps cover the wake_event ->
        # _drain_and_flush path.
        for _ in range(5):
            await asyncio.sleep(0)
    finally:
        await q.stop()

    # All 3 events landed in (typically) one batch — but the test
    # is robust to the flusher waking mid-enqueue and producing
    # multiple batches; what matters is total count.
    total = sum(len(b) for b in flushed_batches)
    assert total == 3, f"expected 3 events flushed; got {total} via {flushed_batches!r}"


@pytest.mark.asyncio
async def test_interval_flush_fires_below_threshold() -> None:
    """Steady-state low-volume tenants must still see their events
    written — the interval timer flushes pending events even when
    the threshold isn't hit."""
    flushed_batches: list[list[dict]] = []

    async def flush(events: list[dict]) -> None:
        flushed_batches.append(events)

    q = AuditEventQueue(
        max_queue_size=100,
        flush_threshold=100,  # never reached in this test
        flush_interval_seconds=0.05,
        flush_callable=flush,
    )
    await q.start()
    try:
        q.enqueue({"only": True})
        # Wait two interval ticks so the flusher has time to wake on
        # the timeout path and flush the single event.
        await asyncio.sleep(0.15)
    finally:
        await q.stop()

    total = sum(len(b) for b in flushed_batches)
    assert total == 1, (
        f"expected the single event to be flushed; got {flushed_batches!r}"
    )


@pytest.mark.asyncio
async def test_stop_drains_pending_events() -> None:
    """Graceful shutdown must flush whatever is in the queue, not just
    cancel the loop and lose pending events."""
    flushed_batches: list[list[dict]] = []

    async def flush(events: list[dict]) -> None:
        flushed_batches.append(events)

    q = AuditEventQueue(
        max_queue_size=100,
        flush_threshold=1000,  # avoid threshold-wake interference
        flush_interval_seconds=10.0,  # avoid interval-wake interference
        flush_callable=flush,
    )
    await q.start()
    q.enqueue({"shutdown_pending": True})
    # No await — go straight to stop() so the event is still in queue.
    await q.stop()

    total = sum(len(b) for b in flushed_batches)
    assert total == 1, (
        f"shutdown drain must flush pending events; got {flushed_batches!r}"
    )


@pytest.mark.asyncio
async def test_overflow_drops_and_increments_counter() -> None:
    """When the queue is full, enqueue drops the new event and bumps
    the dropped counter — must NOT raise (audit isn't on the
    correctness path; dropping > blocking the request hot path)."""

    async def flush(events: list[dict]) -> None:
        pass

    q = AuditEventQueue(
        max_queue_size=2,
        flush_threshold=1000,
        flush_interval_seconds=1000.0,
        flush_callable=flush,
    )
    # Don't start the flusher — we want the queue to actually fill.
    q.enqueue({"i": 0})
    q.enqueue({"i": 1})
    assert q.queue_size == 2
    assert q.dropped_count == 0

    q.enqueue({"i": 2})  # full
    q.enqueue({"i": 3})  # still full

    assert q.queue_size == 2
    assert q.dropped_count == 2


@pytest.mark.asyncio
async def test_flush_callable_failure_does_not_kill_loop() -> None:
    """A flush callable that raises on the first call must not stop
    the flusher — subsequent enqueues should still be flushed when
    the callable starts working again. This is the operational
    invariant: storage-api blips don't compound into total audit
    loss. Also verifies ``flushed_count`` and ``failed_count`` are
    incremented separately so dashboards can distinguish the two."""
    call_count = 0
    flushed_batches: list[list[dict]] = []

    async def flush(events: list[dict]) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated storage-api blip")
        flushed_batches.append(events)

    q = AuditEventQueue(
        max_queue_size=100,
        flush_threshold=1,  # flush on every enqueue
        flush_interval_seconds=10.0,
        flush_callable=flush,
    )
    await q.start()
    try:
        q.enqueue({"will_fail": True})  # triggers raise
        # Give the flusher a chance to log + continue.
        for _ in range(5):
            await asyncio.sleep(0)

        q.enqueue({"will_succeed": True})
        for _ in range(5):
            await asyncio.sleep(0)
    finally:
        await q.stop()

    assert call_count >= 2, "flusher must have made a 2nd attempt after the failure"
    # The second event must reach the success path.
    flat = [e for batch in flushed_batches for e in batch]
    assert any(e.get("will_succeed") for e in flat), (
        f"recovery flush must include the second event; got {flushed_batches!r}"
    )
    # The first event was drained but lost; the second was written.
    # Counters split so dashboards can tell the two apart.
    assert q.failed_count == 1, f"expected 1 failed event; got {q.failed_count}"
    assert q.flushed_count == 1, f"expected 1 flushed event; got {q.flushed_count}"


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    """Re-starting an already-running queue must not double-spawn the
    flusher task — the lifespan re-import path in tests would
    otherwise leak a second loop."""

    async def flush(events: list[dict]) -> None:
        pass

    q = AuditEventQueue(
        max_queue_size=100,
        flush_threshold=10,
        flush_interval_seconds=10.0,
        flush_callable=flush,
    )
    await q.start()
    try:
        first_task = q._flusher_task  # type: ignore[attr-defined]
        await q.start()  # second call
        second_task = q._flusher_task  # type: ignore[attr-defined]
        assert first_task is second_task
    finally:
        await q.stop()


@pytest.mark.asyncio
async def test_drain_chunks_above_chunk_size() -> None:
    """``_drain_and_flush`` must slice the drained list into
    ``chunk_size`` batches so the storage-side ``_MAX_BATCH_SIZE=500``
    cap is never exceeded — even when the queue built up far past
    that during a storage-api outage. With ``chunk_size=2`` and 5
    enqueued events, the flusher must call the callable 3 times
    (sizes 2, 2, 1), not once with size 5."""
    flushed_batches: list[list[dict]] = []

    async def flush(events: list[dict]) -> None:
        flushed_batches.append(list(events))

    q = AuditEventQueue(
        max_queue_size=100,
        flush_threshold=1000,  # avoid threshold-wake interference
        flush_interval_seconds=10.0,  # avoid interval-wake interference
        flush_callable=flush,
        chunk_size=2,
    )
    await q.start()
    for i in range(5):
        q.enqueue({"i": i})
    # Trigger one drain via shutdown — the threshold/interval timers
    # are both far away so this is the cleanest path to flush.
    await q.stop()

    sizes = [len(b) for b in flushed_batches]
    assert sizes == [2, 2, 1], (
        f"expected 3 chunks of sizes [2, 2, 1] from chunk_size=2; got {sizes!r}"
    )
    assert q.flushed_count == 5


@pytest.mark.asyncio
async def test_chunk_failure_does_not_drop_other_chunks() -> None:
    """Per-chunk failure isolation: when chunk 1 of N fails, the
    remaining chunks must still flush. ``_failed_count`` increments
    by the failed chunk's size; ``_flushed_count`` increments by the
    successful chunks' sizes."""
    seen_chunks: list[list[dict]] = []

    async def flush(events: list[dict]) -> None:
        seen_chunks.append(list(events))
        # Fail the second chunk only.
        if len(seen_chunks) == 2:
            raise RuntimeError("simulated mid-drain blip")

    q = AuditEventQueue(
        max_queue_size=100,
        flush_threshold=1000,
        flush_interval_seconds=10.0,
        flush_callable=flush,
        chunk_size=2,
    )
    await q.start()
    for i in range(6):
        q.enqueue({"i": i})
    await q.stop()

    assert len(seen_chunks) == 3, (
        f"all 3 chunks must reach the callable even though chunk 2 raised; "
        f"got {seen_chunks!r}"
    )
    assert q.flushed_count == 4, f"chunks 1+3 flushed = 4 events; got {q.flushed_count}"
    assert q.failed_count == 2, f"chunk 2 failed = 2 events; got {q.failed_count}"


@pytest.mark.asyncio
async def test_stop_with_hung_flusher_falls_back_to_cancel() -> None:
    """If the flush callable hangs past the stop() timeout, the queue
    must cancel the task rather than block shutdown indefinitely.
    Cloud Run will SIGKILL on container-stop timeout otherwise."""

    flush_started = asyncio.Event()

    async def hung_flush(events: list[dict]) -> None:
        flush_started.set()
        await asyncio.sleep(60)  # never returns within the test

    q = AuditEventQueue(
        max_queue_size=100,
        flush_threshold=1,
        flush_interval_seconds=10.0,
        flush_callable=hung_flush,
    )
    await q.start()
    q.enqueue({"hangs": True})
    # Wait for the flusher to actually enter the hung callable.
    await flush_started.wait()
    # stop() must return within a small multiple of its own timeout
    # rather than waiting for the hung sleep.
    await asyncio.wait_for(q.stop(timeout=0.1), timeout=2.0)
