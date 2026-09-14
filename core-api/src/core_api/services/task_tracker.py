"""Track background task outcomes in the database."""

import logging
import traceback
from collections.abc import Coroutine
from typing import Any
from uuid import UUID

from core_api.clients.storage_client import get_storage_client

logger = logging.getLogger(__name__)

_MAX_TRACEBACK_LENGTH = 2000


async def record_task_failure(
    task_name: str,
    memory_id: UUID | None,
    tenant_id: str,
    exc: BaseException,
    *,
    tb: str | None = None,
) -> None:
    """Persist one ``BackgroundTaskLog`` row for a failed background task.

    Split out of ``tracked_task`` (09/02 M-40) so a task that SWALLOWS its own
    exceptions can still be seen. ``tracked_task`` only ever writes a row when
    the coroutine raises, so a handler that catches, logs and returns normally
    reports success to the only table an operator inspects — the failure exists
    as a log line and nothing retries it or knows to.

    Never raises: a failure to record a failure must not become the failure.
    """
    try:
        sc = get_storage_client()
        await sc.add_task_failure(
            {
                "task_name": task_name,
                "memory_id": str(memory_id) if memory_id else None,
                "tenant_id": tenant_id,
                "error_message": str(exc),
                "error_traceback": (tb or traceback.format_exc())[-_MAX_TRACEBACK_LENGTH:],
            }
        )
    except Exception:
        logger.exception(
            "Failed to persist failure record for task %s (memory %s)",
            task_name,
            memory_id,
        )


async def tracked_task(
    coro: Coroutine[Any, Any, Any],
    task_name: str,
    memory_id: UUID | None,
    tenant_id: str,
) -> None:
    """Wrap a fire-and-forget coroutine with failure tracking.

    Only writes a BackgroundTaskLog row when the coroutine fails.
    Successful tasks produce no DB writes, avoiding unbounded table growth.
    """
    try:
        await coro
    except Exception as exc:
        tb = traceback.format_exc()
        logger.exception("Background task %s failed for memory %s", task_name, memory_id)
        await record_task_failure(task_name, memory_id, tenant_id, exc, tb=tb)
