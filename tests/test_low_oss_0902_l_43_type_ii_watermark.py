"""oss-0902-l-43 — the Type-II nightly sweep re-paid for unchanged subjects.

``run_shadow`` accepts a ``since`` watermark and ``select_candidates`` uses it to
skip subjects with nothing new since the previous sweep. The crystallizer never
passed it, so ``since`` was always None and every run issued one LLM call per
subject with >=2 live memories — the same answers, re-bought nightly.

The trap this fix has to avoid is bigger than the bug. The current run reserves
its OWN report row with ``status="running"`` before the sweep body executes, so
``get_latest_report`` frequently returns THIS run. Using that as the watermark
would stamp it at now and skip every subject: an overspend would become a sweep
that silently does nothing. Every guard below exists for that, and every
uncertain case resolves to None ("scan everything") because paying twice is
recoverable on the next run and skipping a changed subject is not.
"""

import pytest

from core_api.services import crystallizer_service as cs

pytestmark = pytest.mark.unit


class _SC:
    def __init__(self, latest):
        self._latest = latest
        self.calls = []

    async def get_latest_report(self, tenant_id, fleet_id=None):
        self.calls.append((tenant_id, fleet_id))
        if isinstance(self._latest, Exception):
            raise self._latest
        return self._latest


async def _wm(latest, report_id="cur"):
    return await cs._type_ii_watermark(_SC(latest), "t1", None, report_id)


async def test_previous_completed_run_supplies_the_watermark():
    got = await _wm({"id": "prev", "completed_at": "2026-09-08T02:00:00Z"})
    assert got == "2026-09-08T02:00:00Z"


async def test_this_runs_own_row_is_never_the_watermark():
    """The reserved 'running' row is usually the latest report. Taking it would
    set the watermark to now and skip every subject."""
    assert await _wm({"id": "cur", "completed_at": "2026-09-09T02:00:00Z"}) is None


async def test_a_crashed_running_row_is_not_terminal():
    """A previous attempt that died leaves status='running' and no
    ``completed_at``; it proves nothing was swept."""
    assert await _wm({"id": "prev", "status": "running", "completed_at": None}) is None


async def test_non_string_completed_at_is_refused():
    """``select_candidates`` compares the watermark to ``created_at`` with ``>``.
    A non-string would raise inside the sweep rather than here."""
    assert await _wm({"id": "prev", "completed_at": 1757376000}) is None
    assert await _wm({"id": "prev", "completed_at": ""}) is None


async def test_no_reports_at_all_scans_everything():
    assert await _wm(None) is None
    assert await _wm("not-a-dict") is None


async def test_lookup_failure_scans_everything_rather_than_skipping():
    """The safe direction. A storage blip must not be able to silence the sweep."""
    assert await _wm(RuntimeError("storage down")) is None


async def test_watermark_is_scoped_to_this_fleet():
    """A different fleet's report would be the wrong clock — it says nothing
    about when THESE subjects were last swept.

    08/14 L-30 + 09/02 L-48: this used to assert only that ``fleet_id`` and
    ``report_type`` were PASSED, and they were — but ``GET /reports/latest``
    accepted both and filtered on neither, so the property named in this
    docstring was false the whole time the test was green. ``report_type`` is
    gone (no such column ever existed); ``fleet_id`` now reaches a WHERE
    clause, and the two tests below assert the EFFECT rather than the call.
    """
    sc = _SC(
        {"id": "prev", "fleet_id": "fleet-a", "completed_at": "2026-09-08T02:00:00Z"}
    )
    assert (
        await cs._type_ii_watermark(sc, "t1", "fleet-a", "cur")
        == "2026-09-08T02:00:00Z"
    )
    assert sc.calls == [("t1", "fleet-a")]


async def test_another_fleets_run_is_not_this_fleets_clock():
    """The harm the scoping exists to prevent, stated as an outcome.

    Fleet B swept an hour ago; fleet A last swept a week ago. Taking B's
    ``completed_at`` makes A skip every subject changed in that week —
    ``select_candidates`` compares it to ``created_at`` with ``>``. Storage
    filters this out when it is given a ``fleet_id``; the guard here is what
    covers the tenant-wide case it cannot (see the next test).
    """
    sc = _SC(
        {"id": "prev", "fleet_id": "fleet-b", "completed_at": "2026-09-09T01:00:00Z"}
    )
    assert await cs._type_ii_watermark(sc, "t1", "fleet-a", "cur") is None


async def test_a_tenant_wide_run_does_not_borrow_a_fleets_clock():
    """The case storage answers across fleets on purpose.

    A tenant-wide run passes no ``fleet_id``, and ``/reports/latest`` without
    one means "any fleet" — it also backs a user-facing "my latest report" that
    has no fleet concept. So the scope check has to happen here, where the run
    knows its own scope.
    """
    sc = _SC(
        {"id": "prev", "fleet_id": "fleet-b", "completed_at": "2026-09-09T01:00:00Z"}
    )
    assert await cs._type_ii_watermark(sc, "t1", None, "cur") is None


def test_the_sweep_actually_passes_the_watermark():
    """The whole defect was a parameter nobody supplied, so pin the call site —
    the helper being correct is worth nothing if it is not wired in."""
    import inspect

    src = inspect.getsource(cs._execute_crystallization)
    assert "since=await _type_ii_watermark(" in src


def test_select_candidates_still_honours_since():
    """Pins the contract this fix depends on: with a watermark, an unchanged
    subject costs no LLM call; without one, everything is scanned."""
    from core_api.services.type_ii_materializer import select_candidates

    bundles = {
        "stale": [
            {"id": "1", "created_at": "2026-09-01T00:00:00Z"},
            {"id": "2", "created_at": "2026-09-02T00:00:00Z"},
        ],
        "fresh": [
            {"id": "3", "created_at": "2026-09-01T00:00:00Z"},
            {"id": "4", "created_at": "2026-09-09T00:00:00Z"},
        ],
    }
    assert select_candidates(bundles, "2026-09-08T00:00:00Z") == ["fresh"]
    assert set(select_candidates(bundles, None)) == {"stale", "fresh"}
