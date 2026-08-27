"""The interviewer sweep's two silent-failure modes.

Both were found chasing an intermittent failure in
``test_interview_async_submit.py``, where a sweep reported ``jobs_processed:
0`` one line after the test had asserted its own job was ``pending``. Neither
fix is proven to be that flake's trigger — that remains unreproduced under
instrumentation — but both are real, both make the sweep lie about its own
outcome, and the first turns exactly that assertion failure from an
uninformative ``assert 0 >= 1`` into a named error.

1. ``run_interview_schedule`` caught every exception from the job sweep and
   returned the zero-initialised counters, so a sweep that raised was
   byte-identical to a sweep that found nothing to do — over a 200.
2. The enabled-tenant list was read from the replica. It is not one field
   among many: it decides which tenants are examined at all, so lag drops a
   whole tenant from the tick rather than returning it something stale.
"""

from __future__ import annotations

import pytest

from core_api.services import interview_service
from core_api.services import tenants as tenants_service


async def test_a_failed_job_sweep_is_distinguishable_from_an_idle_one(monkeypatch):
    """Without ``jobs_sweep_ok`` the two are the same five zeros and a 200 —
    which is how a sweep raising on every hourly tick reads as a quiet queue."""

    async def _no_tenants(*a, **kw):
        return []

    async def _boom(*a, **kw):
        raise RuntimeError("db://user:pw@internal-host/secret path=/etc/x")

    monkeypatch.setattr(interview_service, "list_tenants_with_interviewer_enabled", _no_tenants)
    monkeypatch.setattr(interview_service, "process_pending_interview_jobs", _boom)

    summary = await interview_service.run_interview_schedule()

    assert summary["jobs_processed"] == 0
    assert summary["jobs_sweep_ok"] is False
    assert summary["jobs_sweep_error"] == "RuntimeError"
    # The type is the whole payload: this string goes out in an HTTP response,
    # and an exception's own message may carry a host, URL or request fragment.
    # The full text stays in the log line the service already emits.
    assert "internal-host" not in summary["jobs_sweep_error"]
    assert "/etc/x" not in summary["jobs_sweep_error"]


async def test_a_healthy_idle_sweep_reports_ok(monkeypatch):
    """The other half of the distinction — zeros alone must not imply failure."""

    async def _no_tenants(*a, **kw):
        return []

    monkeypatch.setattr(interview_service, "list_tenants_with_interviewer_enabled", _no_tenants)

    summary = await interview_service.run_interview_schedule()

    assert summary["jobs_processed"] == 0
    assert summary["jobs_sweep_ok"] is True
    assert "jobs_sweep_error" not in summary


@pytest.mark.parametrize(
    "sweep",
    ["process_pending_interview_jobs", "run_interview_schedule"],
)
async def test_both_sweeps_read_the_enabled_tenant_list_from_the_writer(monkeypatch, sweep):
    """``read=False`` on the call that selects which tenants exist for this
    tick. A lagging replica here does not stale one field — it removes a
    tenant, and everything under it, from the sweep entirely, while the
    summary still reports success."""
    seen: list[bool] = []

    # ``*_a`` rather than naming ``db``: the real helper still accepts it
    # positionally, but this spy never touches it, and a dead ``db`` parameter
    # is exactly what tests/test_fixture_hygiene.py refuses — it makes callers
    # request a session that goes nowhere.
    async def _spy(*_a, read: bool = True):
        seen.append(read)
        return []

    monkeypatch.setattr(interview_service, "list_tenants_with_interviewer_enabled", _spy)

    await getattr(interview_service, sweep)()

    assert seen, "the sweep did not consult the enabled-tenant list at all"
    assert all(r is False for r in seen), f"expected read=False (writer), got {seen}"


async def test_the_service_helper_forwards_read_to_the_storage_client(monkeypatch):
    """The seam the sweeps rely on — if this silently drops ``read`` the fix
    above is cosmetic and the sweeps go back to the replica."""
    seen: dict = {}

    class _FakeClient:
        async def list_interviewer_enabled_orgs(self, *, read: bool = True):
            seen["read"] = read
            return ["t1"]

    monkeypatch.setattr(tenants_service, "get_storage_client", lambda: _FakeClient())

    assert await tenants_service.list_tenants_with_interviewer_enabled(read=False) == ["t1"]
    assert seen["read"] is False

    assert await tenants_service.list_tenants_with_interviewer_enabled() == ["t1"]
    assert seen["read"] is True, "default must stay on the replica for display callers"
