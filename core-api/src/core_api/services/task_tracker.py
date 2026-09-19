"""Track background task outcomes in the database."""

import asyncio
import logging
import traceback
import weakref
from collections.abc import Coroutine, MutableMapping
from typing import Any
from uuid import UUID

from core_api.clients.storage_client import get_storage_client

logger = logging.getLogger(__name__)

_MAX_TRACEBACK_LENGTH = 2000

#: Concurrent cancellation-record writes per worker process.
#:
#: Sized to core-storage-api's pool, not to our task count: ``db_pool_size=5``
#: + ``db_max_overflow=5`` is TEN sessions, and ``task_add_failure`` opens its
#: own transaction per row. Shutdown is the worst moment to ignore that — one
#: bulk write in flight schedules ~4 tracked tasks per item against
#: ``BULK_MAX_ITEMS=100``, so ~400 tasks can be cancelled at once, and an
#: unbounded fan-out would put 400 single-row transactions against those ten
#: slots while the instances still serving traffic need them. Cloud Run drains
#: one instance at a time, so this lands on shared capacity.
#:
#: Keyed by running loop for the same reason ``routes/lifecycle.py`` documents:
#: a bare module-level ``Semaphore`` binds to the first loop that awaits it and
#: raises "bound to a different event loop" in any suite running more than one.
_CANCEL_RECORD_CONCURRENCY = 10
_CANCEL_SEMAPHORES: MutableMapping[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _cancel_record_semaphore() -> asyncio.Semaphore:
    """This process's shared budget for cancellation-record writes."""
    loop = asyncio.get_running_loop()
    sem = _CANCEL_SEMAPHORES.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_CANCEL_RECORD_CONCURRENCY)
        _CANCEL_SEMAPHORES[loop] = sem
    return sem


async def record_task_failure(
    task_name: str,
    memory_id: UUID | None,
    tenant_id: str,
    exc: BaseException,
    *,
    tb: str | None = None,
    status: str = "failed",
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
                "status": status,
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

    Writes a ``BackgroundTaskLog`` row when the coroutine fails (``failed``) or
    is cancelled (``cancelled``). Successful tasks produce no DB writes,
    avoiding unbounded table growth.
    """
    try:
        await coro
    except asyncio.CancelledError as exc:
        # OSS 08/14 M-19 / 09/02 M-56 — cancellation must not be silent.
        #
        # ``CancelledError`` is a ``BaseException``, so it walked straight past
        # the ``except Exception`` below and this function returned having
        # written nothing. Shutdown cancels every tracked task, so a write that
        # was already ACKed to the caller lost its enrichment or its embed with
        # no row, no retry and nothing to find it by: for the embed path the
        # daily backfill eventually repairs it, but ``enrichment_pending`` rows
        # have no sweep at all and stayed pending forever.
        #
        # Recorded as ``cancelled``, not ``failed``: nothing raised, and an
        # operator triaging genuine failures should not have a clean shutdown
        # in that list.
        #
        # What the row buys, stated honestly: ``background_task_log`` has no
        # reader anywhere in the repo — one writer, zero queries. So this makes
        # the work recoverable BY HAND today (the ``(tenant_id, status)`` index
        # is the shape such a query wants) and makes the damage countable,
        # which is the number that decides whether an automatic sweep is worth
        # building. The sweep is not in this change.
        #
        # Re-raised, always: swallowing it would break ``task.cancel()``'s
        # contract and leave ``cancel_all_tasks``' wait hanging on a task that
        # never settles.
        # DEBUG, not WARNING: ~400 tasks can be cancelled at once and a line
        # each would be a log-volume incident at the moment the process can
        # least afford one. ``cancel_all_tasks`` logs the count; the row is the
        # durable record. Same reasoning ``usage_service`` gives for
        # ``_METER_FAILURE_LOG_EVERY``.
        logger.debug(
            "Background task %s cancelled for memory %s (tenant %s); recording it",
            task_name,
            memory_id,
            tenant_id,
        )
        async with _cancel_record_semaphore():
            await record_task_failure(
                task_name,
                memory_id,
                tenant_id,
                exc,
                tb="cancelled during shutdown; no traceback",
                status="cancelled",
            )
        raise
    except Exception as exc:
        tb = traceback.format_exc()
        logger.exception("Background task %s failed for memory %s", task_name, memory_id)
        await record_task_failure(task_name, memory_id, tenant_id, exc, tb=tb)
