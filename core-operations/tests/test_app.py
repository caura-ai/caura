"""Contract tests for the lifecycle cron registration in app.py.

These pin two things that are easy to regress: (1) every lifecycle job
is wall-clock aligned (has a ``delay_provider``) so none fire an
immediate boot-time tick, and (2) per-job ``*_run_at_hour`` overrides
are actually threaded through to the scheduler.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from core_operations import app
from core_operations.config import Settings
from core_operations.scheduler import Scheduler, seconds_until_next_utc_hour

_EXPECTED_JOBS = {
    "lifecycle-archive-expired",
    "lifecycle-archive-stale",
    "lifecycle-purge-soft-deleted",
    "lifecycle-crystallize",
    "lifecycle-entity-link",
    "lifecycle-insights",
}


def test_all_lifecycle_jobs_registered_and_wall_clock_aligned(monkeypatch):
    fresh = Scheduler()
    monkeypatch.setattr(app, "scheduler", fresh)

    app._register_scheduled_tasks()

    assert fresh.task_count == len(_EXPECTED_JOBS)
    assert {t.name for t in fresh._tasks} == _EXPECTED_JOBS
    for t in fresh._tasks:
        # Aligned => no immediate boot tick, no drift.
        assert t.delay_provider is not None, f"{t.name} is not wall-clock aligned"
        delay = t.delay_provider()
        assert 0 < delay <= 24 * 3600, f"{t.name} delay out of range: {delay}"


def test_run_at_hour_override_is_threaded_through(monkeypatch):
    # An operator override on the pipeline hour should change when
    # crystallize/entity-link fire.
    monkeypatch.setattr(app.settings, "lifecycle_pipeline_run_at_hour", 5)
    fresh = Scheduler()
    monkeypatch.setattr(app, "scheduler", fresh)

    app._register_scheduled_tasks()

    for name in ("lifecycle-crystallize", "lifecycle-entity-link"):
        task = next(t for t in fresh._tasks if t.name == name)
        now = datetime.now(UTC)
        # delay_provider uses its own now(); compare within a small window.
        assert abs(task.delay_provider() - seconds_until_next_utc_hour(5, now=now)) < 5


@pytest.mark.parametrize(
    "field",
    [
        "lifecycle_archive_run_at_hour",
        "lifecycle_purge_run_at_hour",
        "lifecycle_pipeline_run_at_hour",
        "lifecycle_insights_run_at_hour",
    ],
)
def test_config_rejects_out_of_range_hour(field):
    with pytest.raises(ValidationError, match=r"0\.\.23"):
        Settings(**{field: 24})
    with pytest.raises(ValidationError, match=r"0\.\.23"):
        Settings(**{field: -1})
