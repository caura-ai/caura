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

# ``(task_name, ttl_seconds) -> may_this_process_run_the_tick``. Kept as a
# plain callable rather than a client object so this module stays free of
# any transport: the only implementation today speaks HTTP to core-api,
# but nothing here needs to know that, and the tests substitute a plain
# async function.
LeaseFn = Callable[[str, float], Awaitable[bool]]


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


# Default lease TTL for one aligned tick, in seconds. Sized against the
# two things it sits between: it must comfortably outlive the spread
# between replicas reaching the same tick (measured at 116-300ms in prod
# on 2026-09-16), and it must expire well before the same task's next
# legitimate tick, or it suppresses real work instead of duplicate work.
# The effective value is ``min(this, interval_seconds / 2)`` so the second
# property holds for any cadence without this constant being retuned.
_LEASE_TTL_S: float = 300.0


class Scheduler:
    def __init__(self) -> None:
        self._tasks: list[ScheduledTask] = []
        self._running: list[asyncio.Task[None]] = []
        # Latched on first ``start()`` and never reset. Closes the
        # post-stop register() hole: stop() clears _running, but a later
        # register() + start() would otherwise re-spawn old tasks twice.
        self._started: bool = False
        # Optional cross-process guard; see ``set_lease``. None means no
        # coordination, which is this module's historical behaviour.
        self._lease: LeaseFn | None = None

    def set_lease(self, lease: LeaseFn | None) -> None:
        """Install a cross-process guard for wall-clock aligned ticks.

        ``_started`` guards a double start within ONE process; nothing in
        this module can guard across processes, because the scheduler
        holds no shared state and this service deliberately has no
        datastore. So instance count is fire count: at N replicas every
        aligned task fires N times, all inside the same second. Measured
        in prod on 2026-09-16 at two replicas — all nine scheduled
        endpoints fired twice per tick, and two of them exceeded
        core-api's 45s request budget under the doubled load.

        ``lease(task_name, ttl_s)`` returns True if this process may run
        the tick. It is asked ONCE per aligned tick, after the sleep and
        immediately before ``fn()``, so the question is asked at the
        moment of firing rather than a sleep-length earlier.

        Interval-mode tasks are deliberately NOT leased. They fire an
        immediate tick at startup and then drift by ``fn`` duration, so
        replicas do not converge on a shared instant the way an aligned
        task does — there is no single tick for them to contend over, and
        a lease keyed on the task name would just make one replica the
        permanent winner. Every task this service registers is aligned,
        so today that exclusion is theoretical.

        Must be called before ``start()``; raises otherwise, matching
        ``register``.
        """
        if self._started:
            raise RuntimeError("cannot set a lease after scheduler has started")
        self._lease = lease

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

    async def _claim(self, task: ScheduledTask) -> bool:
        """Ask the lease whether THIS process runs this aligned tick.

        FAILS OPEN on every error path, and that is the whole design: a
        lease is an optimisation over behaviour that already tolerates
        duplicates downstream (the consumer dedup gate and the per-tenant
        activity gate both survive a double fire). Denying a tick because
        the coordinator was unreachable would turn a transient outage
        into a silently skipped nightly sweep — strictly worse than the
        duplicate it was trying to prevent. So an unreachable lease, a
        malformed answer and a raised exception all mean "run".
        """
        if self._lease is None:
            return True
        # Halve the cadence so a lease can never outlive into the next
        # legitimate tick of the same task, whatever that cadence is.
        ttl = min(_LEASE_TTL_S, task.interval_seconds / 2)
        try:
            granted = await self._lease(task.name, ttl)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "tick lease unavailable; running the tick unguarded",
                exc_info=True,
                extra={"task": task.name},
            )
            return True
        if not granted:
            logger.info(
                "tick lease held by another instance; skipping this tick",
                extra={"task": task.name},
            )
        return granted

    async def _run(self, task: ScheduledTask) -> None:
        aligned = task.delay_provider is not None
        while True:
            try:
                if aligned:
                    # Sleep until the next target time, THEN run. Recomputed
                    # each cycle so fn() duration can't drift the schedule.
                    assert task.delay_provider is not None  # narrow for mypy
                    await asyncio.sleep(task.delay_provider())
                    # Asked AFTER the sleep, so the claim is made at the
                    # instant of firing rather than a cadence earlier.
                    # Losing it re-enters the loop, which re-sleeps to the
                    # NEXT occurrence — ``delay_provider`` is guaranteed
                    # strictly-future, so this cannot hot-loop.
                    if not await self._claim(task):
                        continue
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
