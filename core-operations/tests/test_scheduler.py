"""Smoke tests for the asyncio scheduler harness."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from core_operations.scheduler import Scheduler, seconds_until_next_utc_hour


@pytest.mark.asyncio
async def test_register_runs_callable_on_interval():
    s = Scheduler()
    fired = asyncio.Event()
    calls = 0

    async def tick():
        nonlocal calls
        calls += 1
        if calls >= 2:
            fired.set()

    s.register("counter", interval_seconds=0.01, fn=tick)
    await s.start()
    await asyncio.wait_for(fired.wait(), timeout=2.0)
    await s.stop()
    assert calls >= 2


@pytest.mark.asyncio
async def test_failing_tick_does_not_kill_loop():
    s = Scheduler()
    recovered = asyncio.Event()
    calls = 0

    async def tick():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        if calls >= 2:
            recovered.set()

    s.register("flaky", interval_seconds=0.01, fn=tick)
    await s.start()
    await asyncio.wait_for(recovered.wait(), timeout=2.0)
    await s.stop()
    assert calls >= 2


@pytest.mark.asyncio
async def test_interval_must_be_positive():
    s = Scheduler()
    with pytest.raises(ValueError, match="interval_seconds must be > 0"):
        s.register("bad", interval_seconds=0, fn=lambda: asyncio.sleep(0))


@pytest.mark.asyncio
async def test_register_rejects_duplicate_name():
    s = Scheduler()

    async def tick():
        pass

    s.register("dup", interval_seconds=10, fn=tick)
    with pytest.raises(ValueError, match="already registered"):
        s.register("dup", interval_seconds=20, fn=tick)
    assert s.task_count == 1


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_safe_with_no_tasks():
    s = Scheduler()
    await s.stop()  # no-op
    await s.start()
    await s.stop()
    await s.stop()


@pytest.mark.asyncio
async def test_double_start_does_not_spawn_duplicate_tasks():
    s = Scheduler()
    fired = asyncio.Event()
    calls = 0

    async def tick():
        nonlocal calls
        calls += 1
        if calls >= 2:
            fired.set()

    s.register("once", interval_seconds=0.01, fn=tick)
    await s.start()
    await s.start()  # ignored
    await asyncio.wait_for(fired.wait(), timeout=2.0)
    await s.stop()
    # exactly one Task per registration, so calls grew from one loop only
    assert s.task_count == 1


@pytest.mark.asyncio
async def test_double_start_with_no_tasks_is_still_a_no_op_on_second_call(caplog):
    s = Scheduler()
    await s.start()
    with caplog.at_level("WARNING"):
        await s.start()
    await s.stop()
    assert any("scheduler already started" in rec.message for rec in caplog.records), (
        "expected the duplicate-start warning even with zero tasks registered"
    )


@pytest.mark.asyncio
async def test_restart_after_stop_does_not_respawn():
    s = Scheduler()

    async def tick():
        pass

    s.register("only", interval_seconds=10, fn=tick)
    await s.start()
    await s.stop()
    # _started latches; second start() is a no-op (no respawn).
    await s.start()
    assert s._running == []


@pytest.mark.asyncio
async def test_is_healthy_lifecycle():
    s = Scheduler()

    async def long_running():
        await asyncio.sleep(60)

    # No tasks registered → healthy (pre-start / standalone)
    assert s.is_healthy

    s.register("worker", interval_seconds=10, fn=long_running)
    # Registered but not started → not healthy
    assert not s.is_healthy

    await s.start()
    # Running → healthy
    assert s.is_healthy

    await s.stop()
    # Stopped → not healthy (running list cleared)
    assert not s.is_healthy


@pytest.mark.asyncio
async def test_register_after_start_raises():
    s = Scheduler()

    async def tick():
        pass

    s.register("first", interval_seconds=10, fn=tick)
    await s.start()
    try:
        with pytest.raises(RuntimeError, match="after scheduler has started"):
            s.register("second", interval_seconds=10, fn=tick)
    finally:
        await s.stop()


@pytest.mark.asyncio
async def test_register_after_stop_still_raises():
    s = Scheduler()

    async def tick():
        pass

    s.register("first", interval_seconds=10, fn=tick)
    await s.start()
    await s.stop()
    # _started is latched; can't register more tasks even after a stop.
    with pytest.raises(RuntimeError, match="after scheduler has started"):
        s.register("second", interval_seconds=10, fn=tick)


# --- wall-clock alignment ----------------------------------------------


def test_seconds_until_next_utc_hour_same_day():
    now = datetime(2026, 6, 3, 0, 0, 0, tzinfo=UTC)
    # next 02:00 is 2h out
    assert seconds_until_next_utc_hour(2, now=now) == 2 * 3600


def test_seconds_until_next_utc_hour_rolls_to_tomorrow_when_past():
    now = datetime(2026, 6, 3, 5, 30, 0, tzinfo=UTC)
    # 02:00 already passed today → tomorrow 02:00 == 20.5h out
    expected = (24 - 5.5 + 2) * 3600
    assert seconds_until_next_utc_hour(2, now=now) == expected


def test_seconds_until_next_utc_hour_exact_boundary_rolls_forward():
    # Exactly on the target → next occurrence is a full day out, never 0.
    now = datetime(2026, 6, 3, 2, 0, 0, tzinfo=UTC)
    assert seconds_until_next_utc_hour(2, now=now) == 24 * 3600


def test_seconds_until_next_utc_hour_always_positive_and_bounded():
    now = datetime(2026, 6, 3, 1, 59, 59, tzinfo=UTC)
    s = seconds_until_next_utc_hour(2, now=now)
    assert 0 < s <= 24 * 3600
    assert s == 1.0


def test_seconds_until_next_utc_hour_rejects_out_of_range():
    base = datetime(2026, 6, 3, 0, 0, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match=r"0\.\.23"):
        seconds_until_next_utc_hour(24, now=base)
    with pytest.raises(ValueError, match=r"0\.\.23"):
        seconds_until_next_utc_hour(-1, now=base)


@pytest.mark.asyncio
async def test_aligned_task_does_not_fire_immediately_at_startup():
    s = Scheduler()
    calls = 0

    async def tick():
        nonlocal calls
        calls += 1

    # delay_provider returns a long sleep, so within the test window the
    # aligned task should NOT have run even once (unlike interval mode,
    # which fires at the top of the loop).
    s.register("nightly", interval_seconds=24 * 3600, fn=tick, delay_provider=lambda: 100.0)
    await s.start()
    await asyncio.sleep(0.05)
    await s.stop()
    assert calls == 0, "aligned task must wait for delay_provider, not fire at boot"


@pytest.mark.asyncio
async def test_aligned_task_sleeps_then_runs_each_cycle():
    s = Scheduler()
    fired = asyncio.Event()
    calls = 0
    delay_calls = 0

    def delay():
        nonlocal delay_calls
        delay_calls += 1
        return 0.01

    async def tick():
        nonlocal calls
        calls += 1
        if calls >= 2:
            fired.set()

    s.register("nightly", interval_seconds=24 * 3600, fn=tick, delay_provider=delay)
    await s.start()
    await asyncio.wait_for(fired.wait(), timeout=2.0)
    await s.stop()
    assert calls >= 2
    # delay_provider is recomputed before every run (drift-free): one
    # delay() call per tick (+ possibly one in-flight for the next cycle).
    assert delay_calls >= calls


@pytest.mark.asyncio
async def test_aligned_failing_tick_recovers_without_killing_loop():
    s = Scheduler()
    recovered = asyncio.Event()
    calls = 0

    async def tick():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        if calls >= 2:
            recovered.set()

    s.register("nightly", interval_seconds=24 * 3600, fn=tick, delay_provider=lambda: 0.01)
    await s.start()
    await asyncio.wait_for(recovered.wait(), timeout=2.0)
    await s.stop()
    assert calls >= 2


@pytest.mark.asyncio
async def test_cancel_during_sleep_logs_cancelled(caplog):
    s = Scheduler()

    async def tick():
        # Returns immediately so the task spends nearly all its time
        # in the inter-tick asyncio.sleep — that's the path we want
        # cancellation to traverse.
        return

    s.register("napper", interval_seconds=10, fn=tick)
    await s.start()
    # Yield once so the task actually enters its sleep.
    await asyncio.sleep(0.01)
    with caplog.at_level("INFO"):
        await s.stop()
    assert any("scheduled task cancelled" in rec.message for rec in caplog.records), (
        "expected the CancelledError handler to log on shutdown"
    )
