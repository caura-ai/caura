"""Background task tracking for graceful shutdown."""

import asyncio
import inspect
import logging

from common.env_utils import read_float_env

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()

#: Seconds to let in-flight tracked tasks FINISH before cancelling them
#: (OSS 08/14 M-19, 09/02 M-56). Previously zero: shutdown cancelled every task
#: the instant it was reached, so a write already ACKed to the caller lost its
#: embed or its enrichment mid-flight.
#:
#: Sizing, and read this before changing it. ``asyncio.wait`` returns as soon as
#: the tasks finish, so this is a ceiling, not a cost. What it buys depends on
#: the deployment: in DEFERRED mode (``inline_enrichment`` off — the SaaS shape)
#: a task is a single Pub/Sub publish and finishes in milliseconds, so the grace
#: is nearly free and saves the publish. In INLINE mode (the OSS default) the
#: same task is a multi-second LLM call, so 2s often will NOT save it — but
#: inline deployments are also the ones not running under Cloud Run's 10s
#: SIGTERM budget, so they can afford a larger value.
_DRAIN_GRACE_S: float = read_float_env("BACKGROUND_TASK_DRAIN_GRACE_S", 2.0)

#: Bounded wait AFTER cancelling, so each task's ``CancelledError`` handler can
#: write its ``background_task_log`` row (``tracked_task``).
#:
#: 0.5 rather than something roomier, because the distribution is bimodal and
#: neither mode wants more: against healthy storage ~400 bounded writes settle
#: in well under 100ms, and against unhealthy storage the connect-phase retry
#: floor alone is several seconds, so no plausible value here wins that race —
#: it would just spend the termination budget before losing anyway.
#:
#: To make this step free, set BOTH this and ``BACKGROUND_TASK_DRAIN_GRACE_S``
#: to 0: zeroing only the grace still leaves the settle, and the per-task write
#: it waits on did not exist before.
_CANCEL_SETTLE_S: float = read_float_env("BACKGROUND_TASK_CANCEL_SETTLE_S", 0.5)

#: Loop turns ``cancel_all_tasks`` yields for tasks that have not taken their
#: first step. One turn starts every task created before it, so more are needed
#: only when a starting task schedules another; the bound keeps a task that
#: respawns itself forever from holding up shutdown.
_FIRST_STEP_TURNS = 10


def _not_started(task: asyncio.Task) -> bool:
    coro = task.get_coro()
    return (
        not task.done()
        and inspect.iscoroutine(coro)
        and inspect.getcoroutinestate(coro) == inspect.CORO_CREATED
    )


def track_task(coro) -> asyncio.Task:
    """Create a tracked background task. Tracked tasks are cancelled on shutdown."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def cancel_all_tasks() -> None:
    """Let tracked background tasks finish within a grace, then cancel the rest.

    Runs BEFORE the queue-flush steps in ``app.py``'s shutdown, because these
    tasks are producers for those queues — see the ordering note there.
    """
    pending = set(_background_tasks)
    if not pending:
        return

    if _DRAIN_GRACE_S > 0:
        logger.info(
            "Shutting down: waiting up to %ss for %d background tasks to finish",
            _DRAIN_GRACE_S,
            len(pending),
        )
        done, pending = await asyncio.wait(pending, timeout=_DRAIN_GRACE_S)
        # Retrieve results so a task that raised inside the grace is not
        # reported as "exception was never retrieved" at GC. ``asyncio.wait``,
        # unlike ``gather(return_exceptions=True)``, does not consume them.
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                logger.debug("background task failed during shutdown drain", exc_info=task.exception())

    # A task cancelled before its first step has ``CancelledError`` thrown in
    # ahead of its first line: ``tracked_task`` never reaches the handler that
    # writes its ``cancelled`` row, and the coroutine it wraps is dropped
    # unawaited, so the work vanishes with no trace. That is any task created
    # in the turn just before this point — with no grace, whatever was
    # scheduled as shutdown began. Yielding until each has started puts every
    # cancellation inside the code that records it.
    for _ in range(_FIRST_STEP_TURNS):
        if not any(_not_started(task) for task in _background_tasks):
            break
        await asyncio.sleep(0)

    # Re-read rather than trusting the snapshot: tracked coroutines spawn
    # further tracked tasks (``_reembed_memory`` and the bulk re-embed path both
    # call ``track_task``), so anything created DURING the grace or the turns
    # above is in the registry but not in ``pending``. Without this it would be neither
    # cancelled nor awaited, and the storage client would then close under it.
    pending |= _background_tasks - pending

    if not pending:
        return

    logger.info("Shutting down: cancelling %d background tasks", len(pending))
    for task in pending:
        task.cancel()
    # Bounded: each cancelled ``tracked_task`` writes a row on its way out, and
    # that write is a storage round-trip. Waiting unbounded would trade a lost
    # task for a lost shutdown.
    _settled, still_running = await asyncio.wait(pending, timeout=_CANCEL_SETTLE_S)
    if still_running:
        logger.warning(
            "Shutting down: %d background tasks did not settle within %ss; "
            "their interruption was not recorded",
            len(still_running),
            _CANCEL_SETTLE_S,
        )
