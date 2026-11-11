"""Buffered events survive a transient storage failure with stable write keys."""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path

import pytest

from core_api.services import audit_queue
from core_api.services.audit_queue import AuditEventQueue

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_future_retry_does_not_hold_up_new_tenants(monkeypatch) -> None:
    stored = []

    async def no_inline_sleep(delay):
        pytest.fail("an unrelated tenant must not wait for inline retry backoff")

    async def flush(events):
        if events[0]["client_event_id"] == "delete":
            raise RuntimeError("storage unavailable")
        stored.extend(event["client_event_id"] for event in events)

    monkeypatch.setattr(audit_queue, "monotonic", lambda: 0)
    queue = AuditEventQueue(
        max_queue_size=3,
        flush_threshold=3,
        flush_interval_seconds=1,
        flush_callable=flush,
        chunk_size=1,
        retry_initial_backoff_seconds=30,
    )
    assert queue.enqueue({"tenant_id": "a", "client_event_id": "delete"})
    await queue._drain_and_flush()
    assert queue.enqueue({"tenant_id": "a", "client_event_id": "restore"})
    assert queue.enqueue({"tenant_id": "b", "client_event_id": "independent"})
    monkeypatch.setattr(audit_queue.asyncio, "sleep", no_inline_sleep)
    await queue._drain_and_flush()

    assert stored == ["independent"]
    assert queue.queue_size == 2
    assert queue.failed_count == 0
    assert queue._next_retry_delay() == 30, (
        "a blocked deferral must not trigger a busy loop"
    )


async def test_future_retry_does_not_hold_up_another_due_retry(monkeypatch) -> None:
    now = 0
    available = False
    stored = []

    async def no_inline_sleep(delay):
        pytest.fail("a due tenant must not wait for another tenant's future retry")

    async def flush(events):
        if not available:
            raise RuntimeError("storage unavailable")
        stored.extend(event["client_event_id"] for event in events)

    monkeypatch.setattr(audit_queue, "monotonic", lambda: now)
    queue = AuditEventQueue(
        max_queue_size=2,
        flush_threshold=2,
        flush_interval_seconds=1,
        flush_callable=flush,
        chunk_size=1,
    )
    assert queue.enqueue({"tenant_id": "b", "client_event_id": "b"})
    await queue._drain_and_flush()
    now = 1
    assert queue.enqueue({"tenant_id": "a", "client_event_id": "a"})
    await queue._drain_and_flush()
    # B's second failure is due at 3; A's first failure, behind it, is due at 2.
    now = 2
    available = True
    monkeypatch.setattr(audit_queue.asyncio, "sleep", no_inline_sleep)
    await queue._drain_and_flush()
    assert stored == ["a"]
    assert queue.queue_size == 1
    now = 3
    await queue._drain_and_flush()
    assert stored == ["a", "b"]
    assert queue.queue_size == 0
    assert queue.flushed_count == 2
    assert queue.failed_count == 0


async def test_shutdown_flushes_ready_tenant_before_waiting_for_backoff() -> None:
    stored = []

    async def flush(events):
        if events[0]["tenant_id"] == "a":
            raise RuntimeError("storage unavailable")
        stored.extend(event["client_event_id"] for event in events)

    queue = AuditEventQueue(
        max_queue_size=3,
        flush_threshold=10,
        flush_interval_seconds=60,
        flush_callable=flush,
        chunk_size=1,
        retry_initial_backoff_seconds=30,
    )
    assert queue.enqueue({"tenant_id": "a", "client_event_id": "retry"})
    await queue._drain_and_flush()
    for key in ["ready-1", "ready-2"]:
        assert queue.enqueue({"tenant_id": "b", "client_event_id": key})
    await queue.start()
    await asyncio.wait_for(queue.stop(timeout=0.05), timeout=2)
    assert stored == ["ready-1", "ready-2"]
    assert queue.queue_size == 1, (
        "the unattempted retry remains retained on cancellation"
    )
    assert queue.flushed_count == 2
    assert queue.interrupted_count == queue.failed_count == 0


async def test_new_tenant_wakes_background_backoff_below_threshold(monkeypatch) -> None:
    waiting = asyncio.Event()
    delivered = asyncio.Event()

    async def flush(events):
        if events[0]["tenant_id"] == "a":
            raise RuntimeError("storage unavailable")
        delivered.set()

    queue = AuditEventQueue(
        max_queue_size=3,
        flush_threshold=3,
        flush_interval_seconds=60,
        flush_callable=flush,
        retry_initial_backoff_seconds=30,
    )
    assert queue.enqueue({"tenant_id": "a", "client_event_id": "retry"})
    await queue._drain_and_flush()
    original_wait = queue._wait_for_flush

    async def observe_wait(wait_seconds):
        waiting.set()
        await original_wait(wait_seconds)

    monkeypatch.setattr(queue, "_wait_for_flush", observe_wait)
    await queue.start()
    try:
        await waiting.wait()
        assert queue.enqueue({"tenant_id": "b", "client_event_id": "new"})
        assert queue.queue_size < 3
        await asyncio.wait_for(delivered.wait(), timeout=2)
    finally:
        await queue.stop(timeout=0.05)
    assert queue.flushed_count == 1
    assert queue.queue_size == 1
    assert queue.failed_count == 0


@pytest.mark.parametrize("committed_before_failure", [False, True])
async def test_failed_batch_is_retried_without_new_events(
    committed_before_failure: bool,
) -> None:
    """Recovery needs no new traffic, including when only the response was lost."""
    available = False
    attempts: list[list[dict]] = []
    stored: dict[str, dict] = {}
    events = [
        {"tenant_id": "tenant-a", "client_event_id": f"event-{i}", "action": "delete"}
        for i in range(2)
    ]

    async def flush(batch: list[dict]) -> None:
        attempts.append([dict(event) for event in batch])
        if available or committed_before_failure:
            for event in batch:
                stored.setdefault(event["client_event_id"], dict(event))
        if not available:
            raise RuntimeError("temporary storage failure")

    queue = AuditEventQueue(
        max_queue_size=4,
        flush_threshold=4,
        flush_interval_seconds=1,
        flush_callable=flush,
        retry_initial_backoff_seconds=0,
    )
    for event in events:
        assert queue.enqueue(event)

    await queue._drain_and_flush()
    available = True
    await queue._drain_and_flush()

    assert list(stored.values()) == events, (
        "failed events must reach storage after recovery"
    )
    assert len(attempts) >= 2, "even an unacknowledged commit must be retried"
    assert attempts[-1] == events, (
        "retries must preserve the original deduplication keys and payloads"
    )
    assert len(stored) == 2, "a lost response must not append duplicate rows"


async def test_failed_middle_chunk_survives_successful_siblings() -> None:
    """A failed tenant's slice survives while independent tenants progress."""
    available = False
    stored: dict[str, dict] = {}
    events = [
        {
            "tenant_id": f"tenant-{i // 2}",
            "client_event_id": f"event-{i}",
            "action": "delete",
        }
        for i in range(6)
    ]

    async def flush(batch: list[dict]) -> None:
        assert len(batch) <= 2, "recovery must respect the storage batch limit"
        if not available and batch[0]["client_event_id"] == "event-2":
            raise RuntimeError("temporary middle-chunk failure")
        for event in batch:
            stored.setdefault(event["client_event_id"], dict(event))

    queue = AuditEventQueue(
        max_queue_size=6,
        flush_threshold=6,
        flush_interval_seconds=1,
        flush_callable=flush,
        chunk_size=2,
        retry_initial_backoff_seconds=0,
    )
    for event in events:
        assert queue.enqueue(event)

    await queue._drain_and_flush()
    assert set(stored) == {"event-0", "event-1", "event-4", "event-5"}

    available = True
    await queue._drain_and_flush()

    assert stored == {event["client_event_id"]: event for event in events}, (
        "the failed slice must survive a drain that successfully writes its siblings"
    )


@pytest.mark.parametrize("same_drain", [True, False])
async def test_newer_tenant_events_wait_for_retained_failure(same_drain) -> None:
    available = False
    stored = []

    async def flush(events):
        if events[0]["client_event_id"] == "delete" and not available:
            raise RuntimeError("storage unavailable")
        stored.extend(event["client_event_id"] for event in events)

    queue = AuditEventQueue(
        max_queue_size=3,
        flush_threshold=3,
        flush_interval_seconds=1,
        flush_callable=flush,
        chunk_size=1,
        retry_initial_backoff_seconds=0,
    )
    assert queue.enqueue({"tenant_id": "a", "client_event_id": "delete"})
    newer = [
        {"tenant_id": "a", "client_event_id": "restore"},
        {"tenant_id": "b", "client_event_id": "independent"},
    ]
    if same_drain:
        for event in newer:
            assert queue.enqueue(event)
    await queue._drain_and_flush()
    if not same_drain:
        for event in newer:
            assert queue.enqueue(event)
    await queue._drain_and_flush()

    assert stored == ["independent"], "newer events for the failed tenant must wait"
    assert queue.queue_size == 2
    assert queue.failed_count == 0
    available = True
    await queue._drain_and_flush()
    assert stored == ["independent", "delete", "restore"]
    assert queue.queue_size == 0
    assert queue.flushed_count == 3
    assert queue.failed_count == 0


async def test_ordering_deferrals_do_not_spend_flush_attempts() -> None:
    calls = {"delete": 0, "restore": 0}
    stored = []

    async def flush(events):
        key = events[0]["client_event_id"]
        calls[key] += 1
        if calls[key] < 3:
            raise RuntimeError("storage unavailable")
        stored.append(key)

    queue = AuditEventQueue(
        max_queue_size=2,
        flush_threshold=2,
        flush_interval_seconds=1,
        flush_callable=flush,
        chunk_size=1,
        retry_initial_backoff_seconds=0,
    )
    for key in calls:
        assert queue.enqueue({"tenant_id": "a", "client_event_id": key})
    for _ in range(5):
        await queue._drain_and_flush()

    assert stored == ["delete", "restore"]
    assert calls == {"delete": 3, "restore": 3}
    assert queue.flushed_count == 2
    assert queue.failed_count == 0
    assert queue.queue_size == 0


async def test_ordering_deferrals_share_retention_cap(caplog) -> None:
    stored = []
    available = False

    async def flush(events):
        key = events[0]["client_event_id"]
        if key == "delete" and not available:
            raise RuntimeError("storage unavailable")
        if key == "independent":
            assert queue.enqueue({"tenant_id": "b", "client_event_id": "new-1"})
            assert queue.enqueue({"tenant_id": "b", "client_event_id": "new-2"})
        stored.append(key)

    queue = AuditEventQueue(
        max_queue_size=3,
        flush_threshold=3,
        flush_interval_seconds=1,
        flush_callable=flush,
        chunk_size=1,
        retry_initial_backoff_seconds=0,
    )
    for tenant_id, key in [("a", "delete"), ("b", "independent"), ("a", "restore")]:
        assert queue.enqueue({"tenant_id": tenant_id, "client_event_id": key})
    await queue._drain_and_flush()

    assert queue.queue_size == 3
    assert queue.failed_count == 1, (
        "the deferred event cannot exceed retention capacity"
    )
    assert queue.dropped_count == 0
    assert "audit chunk deferral failed" in caplog.text
    available = True
    await queue._drain_and_flush()
    assert stored == ["independent", "delete", "new-1", "new-2"]
    assert queue.queue_size == 0
    assert queue.flushed_count == 4
    assert queue.failed_count == 1


async def test_retry_deadlines_back_off_without_blocking_new_events(
    monkeypatch,
) -> None:
    now = 0.0
    calls = []

    async def flush(events):
        key = events[0]["client_event_id"]
        calls.append(key)
        if key == "old" and calls.count("old") < 3:
            raise RuntimeError("storage unavailable")

    monkeypatch.setattr(audit_queue, "monotonic", lambda: now)
    queue = AuditEventQueue(
        max_queue_size=2,
        flush_threshold=2,
        flush_interval_seconds=1,
        flush_callable=flush,
        retry_initial_backoff_seconds=2,
        retry_max_backoff_seconds=3,
    )
    assert queue.enqueue({"tenant_id": "a", "client_event_id": "old"})
    await queue._drain_and_flush()
    assert queue.enqueue({"tenant_id": "b", "client_event_id": "new"})
    await queue._drain_and_flush()
    assert calls == ["old", "new"]
    assert queue._next_retry_delay() == 2
    now = 2
    await queue._drain_and_flush()
    assert queue._next_retry_delay() == 3, "backoff doubles up to its ceiling"
    now = 4.99
    await queue._drain_and_flush()
    assert calls == ["old", "new", "old"], "retry must not run early"
    now = 5
    await queue._drain_and_flush()
    assert calls == ["old", "new", "old", "old"]
    assert queue.queue_size == 0
    assert queue.failed_count == 0
    assert queue.flushed_count == 2


async def test_persistent_outage_exhausts_attempts_without_growing_queue(
    caplog,
) -> None:
    calls = 0

    async def flush(events):
        nonlocal calls
        calls += 1
        raise RuntimeError("storage unavailable")

    queue = AuditEventQueue(
        max_queue_size=2,
        flush_threshold=2,
        flush_interval_seconds=1,
        flush_callable=flush,
        max_flush_attempts=3,
        retry_initial_backoff_seconds=0,
    )
    for i in range(2):
        assert queue.enqueue({"client_event_id": str(i)})
    await queue._drain_and_flush()
    assert queue.queue_size == 2
    assert queue.failed_count == 0, "retained events have not been lost"
    assert not queue.enqueue({"client_event_id": "overflow"})
    for _ in range(5):
        await queue._drain_and_flush()
        assert queue.queue_size <= 2

    assert calls == 3, "the attempt cap includes the original write"
    assert queue.queue_size == 0
    assert queue.failed_count == 2
    assert queue.dropped_count == 1
    assert queue.flushed_count == 0
    assert sum("events lost" in record.message for record in caplog.records) == 1


async def test_retention_cap_counts_loss_when_new_events_fill_queue(caplog) -> None:
    async def flush(events):
        if events[0]["client_event_id"] == "old":
            # Request tasks can fill the space released by the in-flight drain.
            assert queue.enqueue({"client_event_id": "new-1"})
            assert queue.enqueue({"client_event_id": "new-2"})
            raise RuntimeError("storage unavailable")

    queue = AuditEventQueue(
        max_queue_size=2,
        flush_threshold=2,
        flush_interval_seconds=1,
        flush_callable=flush,
    )
    assert queue.enqueue({"client_event_id": "old"})
    await queue._drain_and_flush()

    assert queue.queue_size == 2
    assert queue.failed_count == 1
    assert queue.dropped_count == 0
    assert "events lost" in caplog.text
    await queue._drain_and_flush()
    assert queue.queue_size == 0
    assert queue.flushed_count == 2


async def test_background_retry_recovers_without_new_traffic() -> None:
    recovered = asyncio.Event()
    calls = 0

    async def flush(events):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary failure")
        recovered.set()

    queue = AuditEventQueue(
        max_queue_size=2,
        flush_threshold=1,
        flush_interval_seconds=0.01,
        flush_callable=flush,
        retry_initial_backoff_seconds=0.01,
    )
    await queue.start()
    try:
        assert queue.enqueue({"client_event_id": "event"})
        await asyncio.wait_for(recovered.wait(), timeout=2)
    finally:
        await queue.stop()
    assert calls == 2
    assert queue.flushed_count == 1
    assert queue.failed_count == 0


async def test_shutdown_respects_attempt_cap() -> None:
    calls = 0

    async def flush(events):
        nonlocal calls
        calls += 1
        raise RuntimeError("storage unavailable")

    queue = AuditEventQueue(
        max_queue_size=1,
        flush_threshold=1,
        flush_interval_seconds=1,
        flush_callable=flush,
        max_flush_attempts=3,
        retry_initial_backoff_seconds=0,
    )
    await queue.start()
    assert queue.enqueue({"client_event_id": "event"})
    await queue.stop(timeout=1)
    assert calls == 3
    assert queue.failed_count == 1
    assert queue.queue_size == 0


async def test_cancel_during_backoff_keeps_unattempted_events(monkeypatch) -> None:
    waiting = asyncio.Event()

    async def flush(events):
        raise RuntimeError("storage unavailable")

    queue = AuditEventQueue(
        max_queue_size=2,
        flush_threshold=2,
        flush_interval_seconds=1,
        flush_callable=flush,
    )
    assert queue.enqueue({"client_event_id": "retry"})
    await queue._drain_and_flush()
    original_wait = queue._wait_for_flush

    async def observe_wait(wait_seconds):
        waiting.set()
        await original_wait(wait_seconds)

    monkeypatch.setattr(queue, "_wait_for_flush", observe_wait)
    await queue.start()
    await waiting.wait()
    assert queue.enqueue({"client_event_id": "new"})
    task = queue._flusher_task
    assert task is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert queue.queue_size == 2
    assert queue.flushed_count == queue.failed_count == queue.interrupted_count == 0


def _production_batch_flusher(flush_one_tenant):
    """Execute the real lifespan closure without starting unrelated services."""
    path = (
        Path(__file__).resolve().parents[1] / "core-api" / "src" / "core_api" / "app.py"
    )
    node = next(
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_flush_audit_batch"
    )
    namespace = {
        "asyncio": asyncio,
        "logger": logging.getLogger(__name__),
        "_UNKNOWN_TENANT_SENTINEL": object(),
        "_flush_one_tenant": flush_one_tenant,
    }
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace
    )
    return namespace["_flush_audit_batch"]


@pytest.mark.parametrize("failed_tenants", [{"b"}, {"a", "b"}])
async def test_partial_tenant_failure_retries_only_failed_events(
    failed_tenants,
) -> None:
    available = False
    calls = []

    async def flush_one_tenant(tenant_id, events):
        calls.append(tenant_id)
        if not available and tenant_id in failed_tenants:
            raise RuntimeError("storage unavailable")

    queue = AuditEventQueue(
        max_queue_size=4,
        flush_threshold=4,
        flush_interval_seconds=1,
        flush_callable=_production_batch_flusher(flush_one_tenant),
        retry_initial_backoff_seconds=0,
    )
    for tenant_id in ["a", "b", "c", None]:
        assert queue.enqueue(
            {"tenant_id": tenant_id, "client_event_id": str(tenant_id)}
        )
    await queue._drain_and_flush()
    assert queue.queue_size == len(failed_tenants)
    assert queue.flushed_count == 3 - len(failed_tenants)
    assert queue.failed_count == 0

    available = True
    await queue._drain_and_flush()
    assert calls == ["a", "b", "c", *sorted(failed_tenants)]
    assert queue.flushed_count == 3, "successful tenants are credited exactly once"
    assert queue.failed_count == 0
    assert queue.queue_size == 0
