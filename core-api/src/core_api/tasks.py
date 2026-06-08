"""Background task tracking for graceful shutdown."""

import asyncio
import logging

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()


def track_task(coro) -> asyncio.Task:
    """Create a tracked background task. Tracked tasks are cancelled on shutdown."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def cancel_all_tasks() -> None:
    """Cancel all tracked background tasks (called on shutdown)."""
    count = len(_background_tasks)
    if count == 0:
        return
    logger.info("Shutting down: cancelling %d background tasks", count)
    for task in list(_background_tasks):
        task.cancel()
    await asyncio.gather(*_background_tasks, return_exceptions=True)
