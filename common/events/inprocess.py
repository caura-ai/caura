"""In-process event bus — asyncio-only, no external deps.

Used in standalone mode and in tests. Dispatches to subscribers via
`asyncio.create_task`, tracking each task so shutdown can await them.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from common.events.base import (
    CircularPublishChainError,
    Event,
    EventBus,
    EventHandler,
)

logger = logging.getLogger(__name__)


class InProcessEventBus(EventBus):
    """Dispatches events to handlers registered in the same process.

    Exceptions raised by a handler are logged but do not propagate to the
    publisher — matches Pub/Sub-style fire-and-forget semantics. A test
    helper (`drain`) is exposed for tests that need to assert handlers
    ran to completion.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._tasks: set[asyncio.Task[None]] = set()

    def subscribe(self, topic: str, handler: EventHandler) -> None:
        self._handlers[topic].append(handler)

    async def publish(self, topic: str, event: Event) -> None:
        for handler in self._handlers.get(topic, ()):
            task = asyncio.create_task(self._safe_invoke(handler, event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _safe_invoke(self, handler: EventHandler, event: Event) -> None:
        try:
            await handler(event)
        except Exception:
            # Don't let a single bad subscriber take down the bus. Log
            # with event metadata so the failure is traceable.
            logger.exception(
                "event handler raised",
                extra={"event_type": event.event_type, "event_id": str(event.event_id)},
            )

    async def drain(self, max_rounds: int = 100) -> None:
        """Await every handler task currently in flight. For tests only —
        production code should await `stop()` during shutdown, not this.

        `max_rounds` guards against circular publish chains (A→B→A) that
        would otherwise keep spawning tasks forever. On exhaustion, the
        remaining tasks are cancelled + awaited before raising so the
        bus doesn't leak live coroutines past this call.
        """
        for _ in range(max_rounds):
            if not self._tasks:
                return
            # Snapshot because new tasks may spawn while we're awaiting.
            pending = list(self._tasks)
            await asyncio.gather(*pending, return_exceptions=True)
        for _ in range(10):
            if not self._tasks:
                break
            snapshot = list(self._tasks)
            for t in snapshot:
                t.cancel()
            await asyncio.gather(*snapshot, return_exceptions=True)
        self._tasks.clear()
        raise CircularPublishChainError(
            "drain() exceeded max_rounds — likely a circular event-publish chain"
        )

    async def stop(self) -> None:
        # `drain()` raises `CircularPublishChainError` on a circular
        # publish chain — catching the dedicated subclass keeps
        # `stop()` safe to call from shutdown paths (lifespan `finally`,
        # signal handlers, test teardown) without masking the real
        # cause. Any other RuntimeError is an unexpected bug and will
        # propagate up the call stack.
        try:
            # stop() wants a bounded cleanup — 3 rounds is enough to
            # absorb ordinary cascades (publish → handler → publish)
            # without turning a bad handler graph into a 100-round
            # shutdown delay.
            await self.drain(max_rounds=3)
        except CircularPublishChainError:
            logger.exception(
                "drain() raised during stop(); handler tasks were cancelled "
                "due to a circular publish chain"
            )
