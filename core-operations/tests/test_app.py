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
    "agent-digest",
    "agent-digest-weekly",
    "interviewer-schedule",
    # Republishes audit rows a fanout wrote but never published a message for.
    # Hourly at half past, offset off the fanout hours it repairs so the sweep's
    # own storage reads do not land in the same instant as the burst it cleans
    # up after.
    "lifecycle-reconcile",
    # Read-only hourly sample of how many live memories are still unembedded.
    # Registered unconditionally, unlike ``embed-backfill``: the count matters
    # most when the sweep is OFF, since nothing is draining the backlog then.
    "embedding-coverage",
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
        # Weekly-aligned jobs are up to 7 days out; daily ones up to 24h.
        max_delay = 7 * 24 * 3600 if t.name == "agent-digest-weekly" else 24 * 3600
        assert 0 < delay <= max_delay, f"{t.name} delay out of range: {delay}"


def test_run_at_hour_override_is_threaded_through(monkeypatch):
    # An operator override on the pipeline hour should change when
    # crystallize fires. Entity-link NO LONGER follows this knob — it has
    # its own, and the test below is the one that pins that.
    monkeypatch.setattr(app.settings, "lifecycle_pipeline_run_at_hour", 5)
    fresh = Scheduler()
    monkeypatch.setattr(app, "scheduler", fresh)

    app._register_scheduled_tasks()

    task = next(t for t in fresh._tasks if t.name == "lifecycle-crystallize")
    now = datetime.now(UTC)
    # delay_provider uses its own now(); compare within a small window.
    assert abs(task.delay_provider() - seconds_until_next_utc_hour(5, now=now)) < 5


def test_entity_link_has_its_own_hour_and_does_not_follow_the_pipeline_knob(monkeypatch):
    """The two heaviest sweeps must be separable.

    Crystallize and entity-link shared ``lifecycle_pipeline_run_at_hour``, so
    no operator could move one off the other's slot. On 2026-09-18 that pairing
    put one staging tenant's cross-link discovery past its 120s storage budget
    and the message dead-lettered after ten 120s attempts.

    Both directions are asserted: moving the entity-link knob must move ONLY
    entity-link, and moving the pipeline knob must not drag it along — a
    default that merely tracked the other setting would pass the first check
    and fail the second.
    """
    monkeypatch.setattr(app.settings, "lifecycle_pipeline_run_at_hour", 2)
    monkeypatch.setattr(app.settings, "lifecycle_entity_link_run_at_hour", 6)
    fresh = Scheduler()
    monkeypatch.setattr(app, "scheduler", fresh)

    app._register_scheduled_tasks()

    now = datetime.now(UTC)
    entity_link = next(t for t in fresh._tasks if t.name == "lifecycle-entity-link")
    crystallize = next(t for t in fresh._tasks if t.name == "lifecycle-crystallize")

    assert abs(entity_link.delay_provider() - seconds_until_next_utc_hour(6, now=now)) < 5, (
        "entity-link did not follow its own hour"
    )
    assert abs(crystallize.delay_provider() - seconds_until_next_utc_hour(2, now=now)) < 5, (
        "moving entity-link must not move crystallize"
    )


@pytest.mark.parametrize(
    "field",
    [
        "lifecycle_archive_run_at_hour",
        "lifecycle_purge_run_at_hour",
        "lifecycle_pipeline_run_at_hour",
        "lifecycle_entity_link_run_at_hour",
        "lifecycle_insights_run_at_hour",
    ],
)
def test_config_rejects_out_of_range_hour(field):
    with pytest.raises(ValidationError, match=r"0\.\.23"):
        Settings(**{field: 24})
    with pytest.raises(ValidationError, match=r"0\.\.23"):
        Settings(**{field: -1})


def test_embed_backfill_not_registered_when_disabled(monkeypatch):
    """Default off: the Pub/Sub topic is Terraform-provisioned, so firing
    before infra lands would just error nightly. Registering conditionally
    (rather than no-opping inside the tick) also keeps ``is_healthy``
    meaningful — there is no runtime slot that could look dead."""
    monkeypatch.setattr(app.settings, "embed_backfill_enabled", False)
    fresh = Scheduler()
    monkeypatch.setattr(app, "scheduler", fresh)

    app._register_scheduled_tasks()

    assert "embed-backfill" not in {t.name for t in fresh._tasks}
    assert fresh.task_count == len(_EXPECTED_JOBS)


def test_embed_backfill_registered_aligned_off_the_congested_hour(monkeypatch):
    """When enabled it is wall-clock aligned and NOT in the 02:00 slot.

    Every other lifecycle tick defaults to 02:00, which is congested enough
    that the nightly cross-link call hits the 120s request ceiling — and this
    sweep feeds the same embedding backend those ticks compete for.
    """
    monkeypatch.setattr(app.settings, "embed_backfill_enabled", True)
    fresh = Scheduler()
    monkeypatch.setattr(app, "scheduler", fresh)

    app._register_scheduled_tasks()

    task = next(t for t in fresh._tasks if t.name == "embed-backfill")
    assert task.delay_provider is not None, "must not fire an immediate boot tick"
    assert 0 < task.delay_provider() <= 24 * 3600
    assert app.settings.embed_backfill_run_at_hour == 4
    assert app.settings.embed_backfill_run_at_hour != app.settings.lifecycle_pipeline_run_at_hour
