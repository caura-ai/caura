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

from datetime import UTC, datetime

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


# ── partial failure: the same lie, one level down ──
#
# ``jobs_sweep_ok`` covers the sweep raising *wholesale*. It cannot cover a
# sweep that fails for SOME tenants, because both loops below catch per-tenant
# (and per-node) exceptions, log them and ``continue`` — so the call returns
# normally and ``jobs_sweep_ok`` stays True. The counters that would have moved
# never move, which is indistinguishable from that tenant having nothing to do.
# Same failure mode #1019 closed at the outer level, and with the same
# consequence: one tenant's interviewer can stop working indefinitely while the
# hourly tick reports a healthy queue. core-operations only logs this summary —
# nothing branches on it — so the log line in core-api is the sole signal today.


class _FailForTenant:
    """Storage stub whose calls raise for one tenant and succeed for the rest."""

    def __init__(self, broken: str, nodes: list[dict] | None = None):
        self._broken = broken
        self._nodes = nodes or []

    def _guard(self, tenant_id: str) -> None:
        if tenant_id == self._broken:
            raise RuntimeError("db://user:pw@internal-host/secret path=/etc/x")

    async def query_documents(self, spec: dict):
        self._guard(str(spec.get("tenant_id")))
        return []

    async def list_nodes(self, tenant_id: str, *a, **kw):
        self._guard(tenant_id)
        return self._nodes

    async def list_commands(self, tenant_id: str, *a, **kw):
        self._guard(tenant_id)
        return []


async def test_a_tenant_the_job_sweep_could_not_query_is_counted_not_swallowed(monkeypatch):
    """A tenant whose pending-jobs query raises is skipped. Without a counter
    the summary is identical to that tenant having an empty queue."""

    async def _tenants(*a, **kw):
        return ["t-ok", "t-broken"]

    monkeypatch.setattr(interview_service, "list_tenants_with_interviewer_enabled", _tenants)
    monkeypatch.setattr(
        interview_service, "get_storage_client", lambda: _FailForTenant("t-broken")
    )

    summary = await interview_service.process_pending_interview_jobs()

    assert summary["tenants"] == 2
    assert summary["jobs_processed"] == 0
    assert summary["tenants_failed"] == 1, (
        "a tenant dropped from the sweep by a storage error must be counted; "
        "otherwise it reads as an idle tenant forever"
    )


async def test_a_tenant_the_schedule_could_not_scan_is_counted_not_swallowed(monkeypatch):
    """The scheduling half has the identical hole in its own tenant loop."""

    async def _tenants(*a, **kw):
        return ["t-ok", "t-broken"]

    async def _settings(_tenant_id):
        return {"interviewer": {"enabled": True}}

    async def _no_jobs(*a, **kw):
        return dict.fromkeys(
            ("jobs_processed", "jobs_done", "jobs_retried", "jobs_parked", "jobs_skipped"), 0
        ) | {"tenants": 0, "tenants_failed": 0}

    monkeypatch.setattr(interview_service, "list_tenants_with_interviewer_enabled", _tenants)
    monkeypatch.setattr(interview_service, "get_settings_for_display", _settings)
    monkeypatch.setattr(interview_service, "process_pending_interview_jobs", _no_jobs)
    monkeypatch.setattr(
        interview_service, "get_storage_client", lambda: _FailForTenant("t-broken")
    )

    summary = await interview_service.run_interview_schedule()

    assert summary["tenants"] == 2
    assert summary["nodes_considered"] == 0
    assert summary["tenants_failed"] == 1


async def test_the_schedule_surfaces_the_job_sweeps_partial_failures_too(monkeypatch):
    """``tenants_failed`` from the jobs sweep is a different population from the
    schedule's own scan failures, so it is copied under its own name rather than
    summed into one ambiguous number."""

    async def _tenants(*a, **kw):
        return []

    async def _partly_failed_sweep(*a, **kw):
        return {
            "tenants": 3,
            "tenants_failed": 2,
            "jobs_processed": 0,
            "jobs_done": 0,
            "jobs_retried": 0,
            "jobs_parked": 0,
            "jobs_skipped": 0,
        }

    monkeypatch.setattr(interview_service, "list_tenants_with_interviewer_enabled", _tenants)
    monkeypatch.setattr(interview_service, "process_pending_interview_jobs", _partly_failed_sweep)

    summary = await interview_service.run_interview_schedule()

    assert summary["jobs_sweep_ok"] is True  # it returned; it did not raise
    assert summary["jobs_tenants_failed"] == 2
    assert summary["tenants_failed"] == 0  # the schedule's own scan was clean


async def test_the_node_counters_add_up(monkeypatch):
    """``nodes_considered`` is incremented before the per-node try, so a node
    whose watermark read raises is counted as considered and then falls through
    without landing in any outcome bucket. The invariant that makes that
    visible: considered == queued + skipped_pending + skipped_not_due + failed."""
    now = datetime.now(UTC).isoformat()
    nodes = [
        {"id": "n-ok", "node_name": "ok", "last_heartbeat": now},
        {"id": "n-broken", "node_name": "broken", "last_heartbeat": now},
    ]

    async def _tenants(*a, **kw):
        return ["t1"]

    async def _settings(_tenant_id):
        return {"interviewer": {"enabled": True}}

    async def _no_jobs(*a, **kw):
        return dict.fromkeys(
            ("jobs_processed", "jobs_done", "jobs_retried", "jobs_parked", "jobs_skipped"), 0
        ) | {"tenants": 0, "tenants_failed": 0}

    # The watermark doc id is a hash of the node id, not the node id itself, so
    # match on the real key rather than a substring that would never fire.
    broken_doc_id = interview_service.watermark_doc_id("n-broken")

    class _Client(_FailForTenant):
        async def get_document(self, _tenant_id, _collection, doc_id, **kw):
            if doc_id == broken_doc_id:
                raise RuntimeError("watermark read failed")
            return None

        async def create_command(self, _payload):
            return {}

    monkeypatch.setattr(interview_service, "list_tenants_with_interviewer_enabled", _tenants)
    monkeypatch.setattr(interview_service, "get_settings_for_display", _settings)
    monkeypatch.setattr(interview_service, "process_pending_interview_jobs", _no_jobs)
    monkeypatch.setattr(
        interview_service, "get_storage_client", lambda: _Client("t-none", nodes=nodes)
    )

    summary = await interview_service.run_interview_schedule()

    assert summary["nodes_considered"] == 2
    assert summary["commands_queued"] == 1
    assert summary["nodes_failed"] == 1
    assert summary["nodes_considered"] == (
        summary["commands_queued"]
        + summary["skipped_pending"]
        + summary["skipped_not_due"]
        + summary["nodes_failed"]
    )


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
