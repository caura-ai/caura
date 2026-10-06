"""A72, third ground — the crystallize tick's cadence is configurable.

It fired once a day at 02:00, which a heavy writing day outruns: everything
written after the tick waits ~24h for the janitor.

The knob alone would be reckless — a bare cadence increase multiplies LLM spend
across every idle tenant. It is affordable only because the activity gate landed
with it (`core-api`: `crystallizer_activity_gate`), which answers a tenant with
no writes since its last completed sweep in two indexed aggregates, without ever
reaching an LLM. The default therefore stays at 24: the cadence is an operator's
decision, not a deploy's.
"""

import inspect
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from core_operations import app
from core_operations.config import settings


def _register_block(job: str) -> str:
    """Just this job's ``scheduler.register(...)`` call.

    Sliced to the NEXT registration rather than a fixed character window: the
    crystallize and entity-link jobs sit adjacent and share a config hour, so a
    loose window reads one job's period as the other's and the assertions below
    silently describe the wrong job.
    """
    src = inspect.getsource(app)
    start = src.index(f'"{job}"')
    nxt = src.find("scheduler.register(", start)
    return src[start : nxt if nxt != -1 else len(src)]


def test_the_default_cadence_is_todays_behaviour():
    """24 hours is one run, exactly as before. A different default here would
    change every deployment's spend silently."""
    assert settings.lifecycle_crystallize_every_hours == 24


def test_the_period_comes_from_the_knob():
    block = _register_block("lifecycle-crystallize")
    assert "_crystallize_hours * 3600" in block
    assert "24 * 3600" not in block, "the hardcoded daily period should be gone"


def test_a_sub_daily_cadence_stops_anchoring_to_one_hour():
    """At 24h the tick is wall-clock aligned to ``lifecycle_pipeline_run_at_hour``.
    Below that the job runs on N-hour slots counted from that hour, so the delay
    provider switches with it rather than silently keeping a daily anchor that no
    longer describes when the job runs."""
    block = _register_block("lifecycle-crystallize")
    assert ">= 24" in block
    assert "seconds_until_next_utc_hour_multiple" in block
    assert "_daily_at" in block


class _Clock:
    """Stands in for ``datetime`` inside the scheduler module, so the registered
    delay provider can be walked forward one fire at a time."""

    current = datetime(2026, 10, 1, 2, 0, 1, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.current


def _fire_times(monkeypatch, every_hours: int, n: int) -> list[datetime]:
    from core_operations import scheduler as scheduler_mod
    from core_operations.scheduler import Scheduler

    monkeypatch.setattr(app.settings, "lifecycle_crystallize_every_hours", every_hours)
    fresh = Scheduler()
    monkeypatch.setattr(app, "scheduler", fresh)
    app._register_scheduled_tasks()
    task = next(t for t in fresh._tasks if t.name == "lifecycle-crystallize")

    monkeypatch.setattr(scheduler_mod, "datetime", _Clock)
    monkeypatch.setattr(_Clock, "current", datetime(2026, 10, 1, 2, 0, 1, tzinfo=UTC))
    fires = []
    for _ in range(n):
        _Clock.current = _Clock.current + timedelta(seconds=task.delay_provider())
        fires.append(_Clock.current)
    return fires


def test_a_six_hour_cadence_fires_four_times_a_day_not_hourly(monkeypatch):
    """The delay provider is what an aligned task sleeps on; the period only
    sizes the lease. A six-hour knob paired with a next-top-of-hour provider
    fired 24 times a day."""
    fires = _fire_times(monkeypatch, 6, 5)
    assert [f.hour for f in fires] == [8, 14, 20, 2, 8]
    assert all(f.minute == 0 and f.second == 0 for f in fires)
    assert {b - a for a, b in pairwise(fires)} == {timedelta(hours=6)}


def test_a_cadence_that_does_not_divide_the_day_stays_evenly_spaced(monkeypatch):
    fires = _fire_times(monkeypatch, 5, 6)
    assert {b - a for a, b in pairwise(fires)} == {timedelta(hours=5)}


def test_the_cadence_floors_at_one_hour():
    """A zero or negative period would busy-loop the scheduler."""
    src = inspect.getsource(app)
    assert "max(1, settings.lifecycle_crystallize_every_hours)" in src


def test_the_other_pipeline_tick_is_untouched():
    """``lifecycle-entity-link`` shares ``lifecycle_pipeline_run_at_hour`` but is
    not part of this retune — it should still be a plain daily job, so the knob
    cannot be mistaken for a pipeline-wide cadence."""
    block = _register_block("lifecycle-entity-link")
    assert "24 * 3600" in block
    assert "_crystallize_hours" not in block
