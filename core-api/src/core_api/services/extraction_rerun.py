"""Re-run entity extraction for memories that lost it.

``background_task_log`` records every extraction that did not end in the
model's graph. ``entity_extraction`` is a run that raised (``failed``) or that a
shutdown stopped (``cancelled``, recorded by ``tracked_task``; since #1992 that
includes a run waiting to ask failing providers again). ``entity_extraction_degraded``
is a run that settled for the regex heuristic after the providers kept failing.
Until this module the table had writers and no reader, so those memories kept a
partial graph, the heuristic's, or none at all, and nothing ran them again.

This is the reader. ``rerun_lost_extractions`` is the hourly sweep
(core-operations' ``entity-extraction-rerun`` job, through
``POST /admin/entity-extraction/rerun-lost``), and ``schedule_rerun`` is what
``POST /admin/memories/{id}/re-extract`` runs for one memory by hand. Both reset
the memory's extraction first (``reset_entity_artifacts``: its links, relations
and any entity left orphaned), so a re-run replaces a partial or heuristic graph
rather than adding to it, then run the same ``process_entity_extraction`` a
write does.

Neither waits for the re-run. With failing providers it can take minutes
(``_extract_asking_again``), so the rows are marked and the re-runs scheduled as
tracked background tasks, a few at a time. A re-run that fails or is cancelled
records a fresh row, which a later sweep picks up; each memory gets at most
``RERUN_MAX_PER_MEMORY`` re-runs, so one whose extraction keeps failing is not
retried every hour until its rows age out.

Tenant by tenant: the storage reads are bound to a tenant, as every storage path
is meant to be (``scripts/tenant_scope_gate.py``), so the sweep lists the active
tenants and reads each one's rows.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections import defaultdict
from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta
from uuid import UUID

from core_api.clients.storage_client import get_storage_client
from core_api.services.entity_extraction_worker import process_entity_extraction
from core_api.services.task_tracker import tracked_task
from core_api.tasks import track_task

logger = logging.getLogger(__name__)

# The ``background_task_log.task_name`` values that mean "this memory does not
# have the model's graph".
LOST_EXTRACTION_TASKS = ("entity_extraction", "entity_extraction_degraded")

# Rows older than this are left alone: a week is long past any outage worth
# recovering from automatically, and it bounds what an hourly sweep reads.
RERUN_LOOKBACK = timedelta(days=7)
# Memories per sweep, oldest row first; a larger backlog drains over later sweeps.
RERUN_MAX_MEMORIES = 50
# Rows read per tenant per sweep. A memory can have several open rows (a
# degraded run and a cancelled one), so this is above RERUN_MAX_MEMORIES.
RERUN_ROWS_PER_TENANT = 100
# Re-runs in flight at once, so recovering from an outage does not hit the
# provider that just came back with a burst.
RERUN_CONCURRENCY = 4
# Re-runs a memory gets before the sweep stops picking it. Storage counts each
# mark of the memory's rows once, however many rows it moved (``handled_at``).
RERUN_MAX_PER_MEMORY = 3
# Tenants whose rows are read at once.
TENANT_READ_CONCURRENCY = 8

# Storage's terminal statuses for a handled row (``TASK_HANDLED_STATUSES``).
RERUN = "rerun"
SKIPPED = "skipped"

# Per event loop, as ``routes.lifecycle._fanout_semaphore`` keeps its budget: a
# semaphore binds to the loop it first waits on.
_RERUN_SLOTS: MutableMapping[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _rerun_slots() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slots = _RERUN_SLOTS.get(loop)
    if slots is None:
        slots = _RERUN_SLOTS[loop] = asyncio.Semaphore(RERUN_CONCURRENCY)
    return slots


async def live_memory(memory_id: str, tenant_id: str) -> dict | None:
    """The memory, from the writer, if it is live; ``None`` if gone, deleted or held.

    A held memory reads as gone, and its release replays extraction.
    """
    memory = await get_storage_client().get_memory(memory_id, tenant_id, read=False)
    if memory is None or memory.get("deleted_at") is not None:
        return None
    return memory


async def _extraction_enabled(tenant_id: str) -> bool:
    from core_api.services.organization_settings import resolve_config

    return bool((await resolve_config(tenant_id)).entity_extraction_enabled)


async def _rerun(memory_id: str, tenant_id: str) -> None:
    """Reset the memory's extraction and run it again, a few at a time.

    The memory is read again once a slot is free, not taken from when the
    re-run was scheduled, since it can wait behind others for many minutes. An
    edit in that time runs its own extraction, and the worker drops a result
    for text the row no longer holds, so resetting and extracting the old text
    would leave the memory with no graph at all. Between this read and the
    reset is one storage call: an edit would have to land and finish its own
    extraction inside it.
    """
    async with _rerun_slots():
        memory = await live_memory(memory_id, tenant_id)
        if memory is None:
            logger.info(
                "entity extraction re-run: memory %s is gone or held by its turn; nothing to do", memory_id
            )
            return
        await get_storage_client().reset_entity_artifacts(tenant_id, memory_id)
        await process_entity_extraction(
            UUID(memory_id),
            tenant_id,
            memory.get("fleet_id"),
            memory.get("agent_id") or "",
            memory.get("content") or "",
            memory.get("memory_type") or "fact",
        )


def _schedule(memory_id: str, tenant_id: str) -> None:
    # ``entity_extraction``, like a write's run: a re-run that raises or is
    # cancelled records a row of that task, which the next sweep picks up.
    track_task(tracked_task(_rerun(memory_id, tenant_id), "entity_extraction", UUID(memory_id), tenant_id))


async def schedule_rerun(memory: dict, tenant_id: str) -> int:
    """Mark the memory's open rows ``rerun`` and schedule its re-run; returns rows marked.

    For ``POST /admin/memories/{id}/re-extract``: the caller has already found
    the memory live and its extraction on. Not capped by
    ``RERUN_MAX_PER_MEMORY``, which only stops the sweep; a manual re-run that
    marked rows does count toward it.
    """
    sc = get_storage_client()
    rows = await sc.list_open_task_failures(
        tenant_id,
        LOST_EXTRACTION_TASKS,
        since=datetime.now(UTC) - RERUN_LOOKBACK,
        limit=RERUN_ROWS_PER_TENANT,
        memory_id=str(memory["id"]),
    )
    marked = await sc.mark_task_failures_handled(tenant_id, [r["id"] for r in rows], RERUN) if rows else 0
    _schedule(str(memory["id"]), tenant_id)
    return marked


async def rerun_lost_extractions() -> dict:
    """The hourly sweep: re-run the oldest lost extractions, across tenants.

    Returns counts: ``tenants`` read, ``unreadable_tenants`` whose rows could not
    be read, ``memories`` picked, how many were ``scheduled`` for a re-run, how
    many ``skipped`` because the memory is gone, held, or its organization has
    extraction off, and ``failed_memories``, picked memories a storage or
    settings error stopped.
    """
    sc = get_storage_client()
    since = datetime.now(UTC) - RERUN_LOOKBACK
    tenants = await sc.list_active_tenants()
    reads = asyncio.Semaphore(TENANT_READ_CONCURRENCY)

    async def _open_rows(tenant_id: str) -> list[dict]:
        async with reads:
            return await sc.list_open_task_failures(
                tenant_id,
                LOST_EXTRACTION_TASKS,
                since=since,
                limit=RERUN_ROWS_PER_TENANT,
                max_reruns_per_memory=RERUN_MAX_PER_MEMORY,
            )

    results = await asyncio.gather(*(_open_rows(t) for t in tenants), return_exceptions=True)
    unreadable = 0
    # (tenant, memory) -> its open rows; the oldest row decides the memory's turn.
    lost: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for tenant_id, result in zip(tenants, results, strict=True):
        if isinstance(result, BaseException):
            unreadable += 1
            logger.warning(
                "entity extraction re-run: could not read tenant %s's rows", tenant_id, exc_info=result
            )
            continue
        for row in result:
            lost[(tenant_id, row["memory_id"])].append(row)

    picked = sorted(lost.items(), key=lambda item: min(r["created_at"] for r in item[1]))[:RERUN_MAX_MEMORIES]
    scheduled = skipped = failed = 0
    for (tenant_id, memory_id), rows in picked:
        ids = [r["id"] for r in rows]
        try:
            memory = await live_memory(memory_id, tenant_id)
            if memory is None or not await _extraction_enabled(tenant_id):
                await sc.mark_task_failures_handled(tenant_id, ids, SKIPPED)
                skipped += 1
                continue
            # Marked before it runs, so the next sweep does not pick it while it
            # is still in flight; its own outcome is a fresh row if it fails.
            if await sc.mark_task_failures_handled(tenant_id, ids, RERUN):
                _schedule(memory_id, tenant_id)
                scheduled += 1
        except Exception:
            # One memory's error must not stop the rest of the sweep, or lose
            # the counts of what it already scheduled. Rows not yet marked stay
            # open, so a later sweep tries the memory again.
            failed += 1
            logger.warning(
                "entity extraction re-run: could not handle memory %s of tenant %s",
                memory_id,
                tenant_id,
                exc_info=True,
            )
    return {
        "tenants": len(tenants),
        "unreadable_tenants": unreadable,
        "memories": len(picked),
        "scheduled": scheduled,
        "skipped": skipped,
        "failed_memories": failed,
    }
