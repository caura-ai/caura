"""Lightweight asyncio scheduler — fixed-interval task harness.

Each registered task runs in its own background asyncio.Task, sleeping
``interval_seconds`` between invocations. Failures are caught, logged,
and the loop continues — one bad tick should not kill the task or
affect peers.

Tasks register at app startup via ``scheduler.register(...)``; the
lifespan calls ``scheduler.start()`` once registrations are in.
Shutdown cancels all tasks and awaits cancellation to propagate.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScheduledTask:
    name: str
    # Wait between consecutive fn() invocations. Effective cadence is
    # ``fn_duration + interval_seconds`` — the loop sleeps AFTER each
    # tick, not on a fixed wall-clock cadence.
    interval_seconds: float
    fn: Callable[[], Awaitable[None]]


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
    ) -> None:
        if self._started:
            raise RuntimeError(
                f"cannot register task {name!r} after scheduler has started"
            )
        if interval_seconds <= 0:
            raise ValueError(
                f"scheduled task {name!r}: interval_seconds must be > 0"
            )
        if any(t.name == name for t in self._tasks):
            raise ValueError(f"scheduled task {name!r} is already registered")
        self._tasks.append(ScheduledTask(name, interval_seconds, fn))

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
                extra={"task": task.name, "interval_s": task.interval_seconds},
            )

    async def stop(self) -> None:
        for t in self._running:
            t.cancel()
        if self._running:
            await asyncio.gather(*self._running, return_exceptions=True)
        self._running.clear()

    async def _run(self, task: ScheduledTask) -> None:
        while True:
            try:
                await task.fn()
                await asyncio.sleep(task.interval_seconds)
            except asyncio.CancelledError:
                logger.info("scheduled task cancelled", extra={"task": task.name})
                raise
            except Exception:
                logger.exception(
                    "scheduled task tick failed; will retry next interval",
                    extra={"task": task.name},
                )
                # Sleep on the exception path too so a failing task waits
                # before retrying instead of hot-looping. Wrapped in its
                # own try so cancellation here also routes through the
                # cancelled-task log line.
                try:
                    await asyncio.sleep(task.interval_seconds)
                except asyncio.CancelledError:
                    logger.info(
                        "scheduled task cancelled",
                        extra={"task": task.name},
                    )
                    raise


scheduler = Scheduler()
