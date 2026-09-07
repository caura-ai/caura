"""One test's fire-and-forget work must not be running during the next one.

``track_task`` registers background tasks in ``core_api.tasks._background_tasks``
and nothing awaits them, while ``asyncio_default_test_loop_scope = session``
keeps a single loop for the whole run. Without the ``_drain_background_tasks``
fixture in ``conftest.py``, a task scheduled by one test keeps running through
the tests that follow it. Measured on this suite with the fixture disabled:
2683 tests started with an earlier test's tasks still running.

What that costs, concretely:

* A leaked task's log records land in a later test's ``caplog``. #1349 went red
  that way, and #1352 and #1353 had to teach four assertions to ignore records
  they never emitted.
* ``tracked_task``'s failure path calls ``get_storage_client()``. Firing after
  ``_patch_storage_client`` restores the original client memoises a REAL client
  into the module singleton, pointed at a storage server no test runs.

THESE TWO TESTS ARE ORDER-DEPENDENT, deliberately and unavoidably: the property
under test is "the previous test's work is not still running", which cannot be
expressed inside a single test. The first schedules work that never finishes on
its own; the second asserts the drain ended it. Keep them adjacent and in this
order. (The suite installs no order-randomising plugin — only pytest-cov,
pytest-asyncio and anyio.)
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = [pytest.mark.unit]


async def test_schedule_a_task_that_outlives_this_test() -> None:
    """Leaks a task on purpose, so the next test can prove it was stopped.

    It waits on an ``Event`` nothing sets, so it cannot finish by itself and
    cannot pass the next test by luck or timing — only cancellation ends it.
    That mirrors the real leak: 2 of the 207 tasks measured outliving their
    test never finished at all, which is why the drain cancels rather than
    waiting.
    """
    from core_api.tasks import _background_tasks, track_task

    never_set = asyncio.Event()

    async def _waits_forever() -> None:
        await never_set.wait()

    task = track_task(_waits_forever())

    # Precondition: the leak this file is about actually exists right now.
    # Without it, the assertion in the next test could pass because nothing
    # was ever scheduled.
    assert not task.done(), "the task finished immediately; it cannot leak"
    assert task in _background_tasks, (
        "track_task did not register the task, so the drain will never see it"
    )


async def test_the_previous_tests_task_is_no_longer_running() -> None:
    """The drain must have ended it before this test began.

    Fails without ``_drain_background_tasks``: the task above waits on an
    Event nobody sets, so it is still pending here and stays pending for the
    rest of the run.
    """
    from core_api.tasks import _background_tasks

    still_running = [task for task in _background_tasks if not task.done()]
    assert still_running == [], (
        "a task scheduled by an earlier test is still running during this one; "
        "its log records, storage calls and failures will be attributed here: "
        f"{[repr(task) for task in still_running]}"
    )
