"""Lightweight asyncio scheduler — interval and wall-clock task harness.

Each registered task runs in its own background asyncio.Task. Two
cadence modes:

* **Interval** (default) — run, then sleep ``interval_seconds``. The
  effective period is ``fn_duration + interval_seconds`` and the first
  tick fires immediately at startup. Good for "every N hours, roughly".
* **Wall-clock aligned** — pass a ``delay_provider``. Before each
  invocation the loop sleeps for ``delay_provider()`` seconds, recomputed
  every cycle so the duration of ``fn()`` never accumulates into drift.
  Use ``seconds_until_next_utc_hour`` to pin a task to a fixed hour of
  day (e.g. an off-peak nightly run). Aligned tasks do NOT fire an
  immediate tick at startup — they wait until the next target time.

Failures are caught, logged, and the loop continues — one bad tick
should not kill the task or affect peers.

Tasks register at app startup via ``scheduler.register(...)``; the
lifespan calls ``scheduler.start()`` once registrations are in.
Shutdown cancels all tasks and awaits cancellation to propagate.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)


def seconds_until_next_utc_hour(hour: int, *, now: datetime | None = None) -> float:
    """Seconds from ``now`` until the next occurrence of ``hour``:00 UTC.

    Always strictly positive and at most 24h: when ``now`` is exactly at
    or past today's target, the result rolls forward to tomorrow. That
    strict-future guarantee is what keeps an aligned task from
    hot-looping after a fast failure — once the target hour is reached,
    the next occurrence is a full day out.

    ``now`` is injectable for tests; it defaults to the current UTC time
    and is expected to be timezone-aware UTC.
    """
    if not 0 <= hour <= 23:
        raise ValueError(f"hour must be in 0..23, got {hour}")
    current = now or datetime.now(UTC)
    target = current.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return (target - current).total_seconds()


def seconds_until_next_utc_top_of_hour(*, now: datetime | None = None) -> float:
    """Seconds from ``now`` until the next :00 UTC of any hour.

    Same strict-future guarantee as :func:`seconds_until_next_utc_hour`
    (always positive, at most 1h) for hourly-cadence jobs — keeps an
    aligned task from hot-looping after a fast failure at the top of the
    hour.
    """
    current = now or datetime.now(UTC)
    target = current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return (target - current).total_seconds()


def seconds_until_next_utc_weekday_hour(weekday: int, hour: int, *, now: datetime | None = None) -> float:
    """Seconds from ``now`` until the next ``weekday``@``hour``:00 UTC.

    ``weekday`` is Python's ``date.weekday()`` convention: 0=Monday … 6=Sunday.
    Same strict-future guarantee as :func:`seconds_until_next_utc_hour` (always
    positive, at most 7 days): when ``now`` is exactly at or past this week's
    target slot, it rolls forward a full week — so an aligned weekly task can't
    hot-loop after a fast failure.

    ``now`` is injectable for tests; defaults to the current UTC time.
    """
    if not 0 <= weekday <= 6:
        raise ValueError(f"weekday must be in 0..6 (Mon..Sun), got {weekday}")
    if not 0 <= hour <= 23:
        raise ValueError(f"hour must be in 0..23, got {hour}")
    current = now or datetime.now(UTC)
    target = current.replace(hour=hour, minute=0, second=0, microsecond=0)
    target += timedelta(days=(weekday - current.weekday()) % 7)
    if target <= current:  # today IS the weekday but the hour has passed
        target += timedelta(days=7)
    return (target - current).total_seconds()


@dataclass(frozen=True)
class ScheduledTask:
    name: str
    # Interval-mode wait between consecutive fn() invocations. Effective
    # cadence is ``fn_duration + interval_seconds`` — the loop sleeps
    # AFTER each tick. When ``delay_provider`` is set this is unused for
    # firing (kept as the nominal/documented period only).
    interval_seconds: float
    fn: Callable[[], Awaitable[None]]
    # When set, the task is wall-clock aligned: before each invocation
    # the loop sleeps ``delay_provider()`` seconds (recomputed each cycle,
    # so it never drifts) and there is no immediate boot-time tick.
    # ``None`` → legacy run-then-sleep interval cadence.
    delay_provider: Callable[[], float] | None = None


class Scheduler:
    def __init__(self) -> None:
        self._tasks: list[ScheduledTask] = []
        self._running: list[asyncio.Task[None]] = []
        # Latched on first ``start()`` and never reset. Closes the
        # post-stop register() hole: stop() clears _running, but a later
        # register() + start() would otherwise re-spawn old tasks twice.
        self._started: bool = False

    def register(
        self,
        name: str,
        interval_seconds: float,
        fn: Callable[[], Awaitable[None]],
        *,
        delay_provider: Callable[[], float] | None = None,
    ) -> None:
        if self._started:
            raise RuntimeError(f"cannot register task {name!r} after scheduler has started")
        if interval_seconds <= 0:
            raise ValueError(f"scheduled task {name!r}: interval_seconds must be > 0")
        if any(t.name == name for t in self._tasks):
            raise ValueError(f"scheduled task {name!r} is already registered")
        self._tasks.append(ScheduledTask(name, interval_seconds, fn, delay_provider))

    @property
    def task_count(self) -> int:
        return len(self._tasks)

    @property
    def is_healthy(self) -> bool:
        # No registered tasks → healthy; otherwise every registration
        # must have a still-live runtime slot.
        if not self._tasks:
            return True
        if len(self._running) != len(self._tasks):
            return False
        return all(not t.done() for t in self._running)

    async def start(self) -> None:
        # Latched flag rather than ``if self._running`` because the
        # latter is empty both pre-start AND between stop()/start()
        # cycles, so checking _running would miss both double-start
        # (with zero registered tasks) and accidental restart.
        if self._started:
            logger.warning("scheduler already started; ignoring duplicate start()")
            return
        self._started = True
        for task in self._tasks:
            t = asyncio.create_task(self._run(task), name=f"sched/{task.name}")
            self._running.append(t)
            logger.info(
                "scheduled task started",
                extra={
                    "task": task.name,
                    "interval_s": task.interval_seconds,
                    "aligned": task.delay_provider is not None,
                },
            )

    async def stop(self) -> None:
        for t in self._running:
            t.cancel()
        if self._running:
            await asyncio.gather(*self._running, return_exceptions=True)
        self._running.clear()

    async def _run(self, task: ScheduledTask) -> None:
        aligned = task.delay_provider is not None
        while True:
            try:
                if aligned:
                    # Sleep until the next target time, THEN run. Recomputed
                    # each cycle so fn() duration can't drift the schedule.
                    assert task.delay_provider is not None  # narrow for mypy
                    await asyncio.sleep(task.delay_provider())
                await task.fn()
                if not aligned:
                    await asyncio.sleep(task.interval_seconds)
            except asyncio.CancelledError:
                logger.info("scheduled task cancelled", extra={"task": task.name})
                raise
            except Exception:
                logger.exception(
                    "scheduled task tick failed; will retry next cycle",
                    extra={"task": task.name},
                )
                if aligned:
                    # The loop top re-sleeps to the next target occurrence,
                    # which is ~a full day out once today's slot has passed
                    # (see seconds_until_next_utc_hour) — no hot-loop risk,
                    # so just fall through and let the loop recompute.
                    continue
                # Interval tasks already fired fn() at the top of the loop,
                # so without an explicit sleep a persistently-failing task
                # would hot-loop. Wrapped in its own try so cancellation
                # here also routes through the cancelled-task log line.
                try:
                    await asyncio.sleep(task.interval_seconds)
                except asyncio.CancelledError:
                    logger.info(
                        "scheduled task cancelled",
                        extra={"task": task.name},
                    )
                    raise


scheduler = Scheduler()
