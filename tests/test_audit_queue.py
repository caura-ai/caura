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

    assert q.enqueue({"i": 2}) is False  # full → dropped
    assert q.enqueue({"i": 3}) is False  # still full

    assert q.queue_size == 2
    assert q.dropped_count == 2


@pytest.mark.asyncio
async def test_enqueue_returns_true_when_queued() -> None:
    """enqueue reports success so a compliance-critical caller can tell a
    queued event from a dropped one (see log_action critical fallback)."""

    async def flush(events: list[dict]) -> None:
        pass

    q = AuditEventQueue(
        max_queue_size=2,
        flush_threshold=1000,
        flush_interval_seconds=1000.0,
        flush_callable=flush,
    )
    assert q.enqueue({"i": 0}) is True
    assert q.enqueue({"i": 1}) is True
    assert q.enqueue({"i": 2}) is False  # now full


@pytest.mark.asyncio
async def test_enqueue_silent_drop_does_not_count_or_warn() -> None:
    """A ``silent=True`` full-queue rejection returns False but leaves the
    drop counter untouched — the critical path recovers the event via a sync
    write, so counting it as dropped would over-count the metric and emit a
    false 'dropped' warning for an event that was never lost."""

    async def flush(events: list[dict]) -> None:
        pass

    q = AuditEventQueue(
        max_queue_size=1,
        flush_threshold=1000,
        flush_interval_seconds=1000.0,
        flush_callable=flush,
    )
    assert q.enqueue({"i": 0}) is True
    # Full now: a silent reject doesn't move the counter; a loud one does.
    assert q.enqueue({"i": 1}, silent=True) is False
    assert q.dropped_count == 0
    assert q.enqueue({"i": 2}) is False
    assert q.dropped_count == 1


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


@pytest.mark.asyncio
async def test_log_action_mints_unique_client_event_id() -> None:
    """log_action stamps a per-event ``client_event_id`` (a fresh UUID) so the
    storage-side bulk flush can dedup a retried (lost-ack) batch instead of
    double-appending to the tamper-evident chain."""
    from unittest.mock import MagicMock, patch
    from uuid import UUID

    from core_api.services import audit_service

    captured: list[dict] = []

    def _capture(payload: dict, *, silent: bool = False) -> bool:
        captured.append(payload)
        return True

    fake_queue = MagicMock()
    fake_queue.enqueue = _capture
    with patch.object(audit_service, "get_audit_queue", return_value=fake_queue):
        await audit_service.log_action(tenant_id="t", action="create", resource_type="memory"
        )
        await audit_service.log_action(tenant_id="t", action="create", resource_type="memory"
        )

    assert len(captured) == 2
    # Present and a valid UUID on each event...
    UUID(captured[0]["client_event_id"])
    UUID(captured[1]["client_event_id"])
    # ...and distinct per event, so two different mutations never collide.
    assert captured[0]["client_event_id"] != captured[1]["client_event_id"]


@pytest.mark.asyncio
async def test_log_action_critical_falls_back_to_sync_on_full_queue() -> None:
    """A ``critical`` event whose enqueue is rejected (queue full → enqueue
    returns False) falls back to a synchronous storage POST instead of being
    silently dropped. Non-critical events stay fire-and-forget."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from core_api.services import audit_service

    full_queue = MagicMock()
    full_queue.enqueue = MagicMock(return_value=False)  # always "full"
    fake_storage = MagicMock()
    fake_storage.create_audit_log = AsyncMock()

    with (
        patch.object(audit_service, "get_audit_queue", return_value=full_queue),
        patch.object(audit_service, "get_storage_client", return_value=fake_storage),
    ):
        # Non-critical: dropped on overflow, no synchronous write.
        await audit_service.log_action(tenant_id="t", action="create", resource_type="memory"
        )
        fake_storage.create_audit_log.assert_not_called()

        # Critical: falls back to the synchronous storage write.
        await audit_service.log_action(tenant_id="t",
            action="nonbusiness_pregate_drop",
            resource_type="memory",
            critical=True,
        )
    fake_storage.create_audit_log.assert_awaited_once()
    payload = fake_storage.create_audit_log.await_args.args[0]
    assert payload["action"] == "nonbusiness_pregate_drop"
    # The non-critical enqueue must NOT be silent (a real drop should count +
    # warn); the critical one MUST be silent (it's recovered via the sync
    # write, so it isn't a real drop and shouldn't inflate the metric).
    silent_flags = [c.kwargs.get("silent", False) for c in full_queue.enqueue.call_args_list]
    assert silent_flags == [False, True]


@pytest.mark.asyncio
async def test_log_action_critical_uses_queue_when_not_full() -> None:
    """A ``critical`` event that the queue accepts (enqueue returns True) does
    NOT pay the synchronous fallback — the hot path stays fast."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from core_api.services import audit_service

    ok_queue = MagicMock()
    ok_queue.enqueue = MagicMock(return_value=True)
    fake_storage = MagicMock()
    fake_storage.create_audit_log = AsyncMock()

    with (
        patch.object(audit_service, "get_audit_queue", return_value=ok_queue),
        patch.object(audit_service, "get_storage_client", return_value=fake_storage),
    ):
        await audit_service.log_action(tenant_id="t",
            action="nonbusiness_pregate_drop",
            resource_type="memory",
            critical=True,
        )
    ok_queue.enqueue.assert_called_once()
    fake_storage.create_audit_log.assert_not_called()


@pytest.mark.asyncio
async def test_log_action_critical_sync_failure_does_not_propagate() -> None:
    """If the critical sync fallback ALSO fails (queue full AND storage down),
    log_action logs loudly but does NOT raise. A critical audit is emitted
    right before a governance enforcement action (a 4xx reject or a
    soft-delete); a failing audit write must not block that action — otherwise
    the row the policy means to drop would be left in place."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from core_api.services import audit_service

    full_queue = MagicMock()
    full_queue.enqueue = MagicMock(return_value=False)  # queue full
    fake_storage = MagicMock()
    fake_storage.create_audit_log = AsyncMock(side_effect=RuntimeError("storage down"))

    with (
        patch.object(audit_service, "get_audit_queue", return_value=full_queue),
        patch.object(audit_service, "get_storage_client", return_value=fake_storage),
    ):
        # Must NOT raise despite both the queue and the sync write failing.
        await audit_service.log_action(tenant_id="t",
            action="nonbusiness_pregate_drop",
            resource_type="memory",
            critical=True,
        )
    fake_storage.create_audit_log.assert_awaited_once()
