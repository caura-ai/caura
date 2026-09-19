"""Shutdown must not lose in-flight background work silently.

OSS 08/14 M-19 and 09/02 M-56 (one shape, two filings): ``cancel_all_tasks``
cancelled every tracked task the instant it was reached, and because
``CancelledError`` is a ``BaseException`` it walked past ``tracked_task``'s
``except Exception`` — so a write already ACKed to the caller lost its embed or
its enrichment with no row, no retry and nothing to find it by. The embed path
is eventually repaired by the backfill; ``enrichment_pending`` rows have no
sweep at all.

The accounting halves of this batch live with the code they belong to:
``tests/test_audit_queue.py`` (L-11) and ``tests/test_usage_meter.py`` (L-44).

These drive the real ``cancel_all_tasks`` and patch the module constants it
actually reads, rather than passing overrides — so they exercise the values
production runs with, and fail if those are removed.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from core_api import tasks as tasks_mod

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def task_sc(monkeypatch):
    """Spy on the storage client ``task_tracker`` writes cancellation rows to.

    Deliberately does NOT touch ``core_api.tasks._background_tasks``: the
    autouse ``_drain_background_tasks`` in conftest already isolates the
    registry, and clearing it here would tear down first and UNREGISTER any
    leaked task before that drain could cancel it — defeating the fixture that
    exists because 207 tasks once outlived their tests.
    """
    client = AsyncMock()
    monkeypatch.setattr(
        "core_api.services.task_tracker.get_storage_client", lambda: client
    )
    return client


async def _run_until_started(coro_fn, *args):
    """Start ``tracked_task`` over a coroutine that signals then blocks."""
    from core_api.services.task_tracker import tracked_task

    started = asyncio.Event()

    async def blocks_forever() -> None:
        started.set()
        await asyncio.sleep(3600)

    task = asyncio.create_task(tracked_task(blocks_forever(), *args))
    await started.wait()
    return task


async def test_a_cancelled_task_records_a_cancelled_row(task_sc) -> None:
    """Shutdown-cancelled work must not vanish.

    ``background_task_log`` is the only table an operator inspects, and this
    wrote nothing to it. The row does not repair the work — nothing reads that
    table yet — but it makes it recoverable by hand and, just as importantly,
    makes the damage countable.
    """
    task = await _run_until_started(None, "background_enrichment", None, "tenant-a")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled(), "the task must report cancelled, not merely finished"
    task_sc.add_task_failure.assert_awaited_once()
    row = task_sc.add_task_failure.await_args.args[0]
    assert row["status"] == "cancelled", "a shutdown is not a failure"
    assert row["task_name"] == "background_enrichment"
    assert row["tenant_id"] == "tenant-a"


async def test_shutdown_lets_an_almost_finished_task_complete(
    task_sc, monkeypatch
) -> None:
    """The grace is the point: work one await from done should land."""
    monkeypatch.setattr(tasks_mod, "_DRAIN_GRACE_S", 2.0)
    finished = False

    async def nearly_done() -> None:
        nonlocal finished
        await asyncio.sleep(0.05)
        finished = True

    tasks_mod.track_task(nearly_done())
    await tasks_mod.cancel_all_tasks()
    assert finished, "a task that needed 50ms was cancelled by a 2s grace"


async def test_shutdown_still_cancels_a_task_that_outlives_the_grace(
    task_sc, monkeypatch
) -> None:
    """The grace is bounded, not a promise to wait forever."""
    monkeypatch.setattr(tasks_mod, "_DRAIN_GRACE_S", 0.05)
    monkeypatch.setattr(tasks_mod, "_CANCEL_SETTLE_S", 1.0)
    finished = False

    async def far_too_slow() -> None:
        nonlocal finished
        await asyncio.sleep(3600)
        finished = True

    task = tasks_mod.track_task(far_too_slow())
    await tasks_mod.cancel_all_tasks()
    assert not finished
    assert task.cancelled(), "shutdown must not wait on it indefinitely"


async def test_a_task_spawned_during_the_grace_is_still_cancelled(
    task_sc, monkeypatch
) -> None:
    """Tracked coroutines spawn further tracked tasks while they run.

    ``_reembed_memory`` and the bulk re-embed path both call ``track_task``
    from inside a tracked task. A task created DURING the grace lands in the
    registry but not in the snapshot taken before it — so without a re-read it
    is neither cancelled nor awaited, and the storage client is then closed out
    from under it.
    """
    monkeypatch.setattr(tasks_mod, "_DRAIN_GRACE_S", 0.2)
    monkeypatch.setattr(tasks_mod, "_CANCEL_SETTLE_S", 1.0)
    spawned: list[asyncio.Task] = []

    async def child() -> None:
        await asyncio.sleep(3600)

    async def parent() -> None:
        await asyncio.sleep(0.02)
        spawned.append(tasks_mod.track_task(child()))

    tasks_mod.track_task(parent())
    await tasks_mod.cancel_all_tasks()

    assert spawned, "the parent never spawned its child"
    assert spawned[0].cancelled(), (
        "a task spawned during the grace escaped cancellation entirely"
    )
