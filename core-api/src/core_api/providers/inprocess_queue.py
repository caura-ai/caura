"""In-process async job queue backed by stdlib asyncio.

Implements the ``JobQueue`` protocol using fire-and-forget
``asyncio.Task`` objects.  Suitable for single-process deployments
where an external broker (Redis / RabbitMQ) is not needed.

Pre-CAURA-655 this module also exposed a cron-style ``schedule()``
method backed by an internal ``_ticker`` task. The only caller was
the in-process lifecycle scheduler that has since moved to
``core-operations``; the scheduler bits are gone.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


class InProcessQueue:
    """Async job queue that runs work in the current event loop."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    async def enqueue(self, func: Any, *args: Any, **kwargs: Any) -> str:
        """Fire-and-forget *func* as a background task."""
        job_id = str(uuid.uuid4())
        task = asyncio.create_task(func(*args, **kwargs))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        logger.debug("Enqueued job %s", job_id)
        return job_id

    async def shutdown(self) -> None:
        """Cancel all tracked tasks."""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
