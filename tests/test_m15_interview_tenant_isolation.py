"""09/02 M-15 — one tenant's settings read could abort the whole sweep.

`run_interview_schedule` guards its per-tenant work in a try/except that logs,
increments `tenants_failed`, and continues. The settings read sat OUTSIDE that
guard:

    for tenant_id in tenants:
        settings = await get_settings_for_display(tenant_id)   # unguarded
        period_hours = int(cfg.get("period_hours") or 12)      # raises on junk
        try:
            nodes = await sc.list_nodes(tenant_id)
            ...

So one unreadable tenant — or one with a non-numeric `period_hours`, which
`int()` raises on — propagated out of the loop and out of the function.

Two consequences, the second worse than the first:

1. every REMAINING tenant went unscheduled for that tick;
2. the persisted-jobs sweep below the loop was skipped entirely. That sweep
   (#665) is the durable retry path for jobs whose fire-and-forget processing
   died at submit time — the one thing that must not be taken out by a
   failure, since failure is precisely what it exists to recover from.

The isolation was already the documented intent: the per-node handler says "one
node's storage failure must not abort scheduling for the tenant's remaining
nodes (or later tenants)", and `tenants_failed` already existed to count it.
Only the settings read was outside the try.
"""

from __future__ import annotations

import pytest

from core_api.services import interview_service

pytestmark = pytest.mark.unit


def _wire(monkeypatch, *, tenants, settings_for):
    """Minimal sweep harness: named tenants, a settings fn, no nodes."""
    calls: dict = {"jobs_swept": False, "settings_seen": []}

    async def _tenants(*a, **kw):
        return list(tenants)

    async def _settings(tenant_id, *a, **kw):
        calls["settings_seen"].append(tenant_id)
        return settings_for(tenant_id)

    async def _jobs(*a, **kw):
        calls["jobs_swept"] = True
        return {}

    class _SC:
        async def list_nodes(self, *a, **kw):
            return []

        async def list_commands(self, *a, **kw):
            return []

    monkeypatch.setattr(
        interview_service, "list_tenants_with_interviewer_enabled", _tenants
    )
    monkeypatch.setattr(interview_service, "get_settings_for_display", _settings)
    monkeypatch.setattr(
        interview_service, "process_pending_interview_jobs", _jobs, raising=False
    )
    monkeypatch.setattr(interview_service, "get_storage_client", lambda: _SC())
    return calls


async def test_an_unreadable_tenant_does_not_abort_the_others(monkeypatch):
    """The headline. ``bad`` is scheduled first, so before the fix nothing
    after it ran at all."""

    def _settings(tenant_id):
        if tenant_id == "bad":
            raise RuntimeError("settings read failed")
        return {"interviewer": {"period_hours": 12}}

    calls = _wire(
        monkeypatch, tenants=["bad", "good-1", "good-2"], settings_for=_settings
    )
    summary = await interview_service.run_interview_schedule()

    assert summary["tenants_failed"] == 1
    assert calls["settings_seen"] == ["bad", "good-1", "good-2"]


async def test_an_unreadable_tenant_does_not_skip_the_durable_jobs_sweep(monkeypatch):
    """The worse half. The persisted-jobs sweep is the retry path for work that
    ALREADY failed once; a tenant failure must not be what prevents it."""

    def _settings(tenant_id):
        raise RuntimeError("settings read failed")

    calls = _wire(monkeypatch, tenants=["bad"], settings_for=_settings)
    summary = await interview_service.run_interview_schedule()

    assert calls["jobs_swept"] is True
    assert summary["jobs_sweep_ok"] is True


async def test_a_non_numeric_period_hours_is_contained(monkeypatch):
    """``int(cfg.get("period_hours") or 12)`` raises ValueError on junk. Tenant
    settings are operator-editable, so this is reachable without any storage
    fault at all."""

    def _settings(tenant_id):
        if tenant_id == "bad":
            return {"interviewer": {"period_hours": "twelve"}}
        return {"interviewer": {"period_hours": 12}}

    calls = _wire(monkeypatch, tenants=["bad", "good"], settings_for=_settings)
    summary = await interview_service.run_interview_schedule()

    assert summary["tenants_failed"] == 1
    assert calls["settings_seen"] == ["bad", "good"]
    assert calls["jobs_swept"] is True


async def test_a_healthy_sweep_reports_no_tenant_failures(monkeypatch):
    """Guard against the fix swallowing everything into ``tenants_failed``."""

    calls = _wire(
        monkeypatch,
        tenants=["a", "b"],
        settings_for=lambda t: {"interviewer": {"period_hours": 12}},
    )
    summary = await interview_service.run_interview_schedule()

    assert summary["tenants_failed"] == 0
    assert summary["tenants"] == 2
    assert calls["jobs_swept"] is True


def test_the_settings_read_is_inside_the_guarded_block():
    """Structural. The behavioural tests above would also pass if someone
    wrapped the whole loop body in a broader try — this pins that the settings
    read shares the EXISTING per-tenant guard, which is what keeps
    ``tenants_failed`` meaningful."""
    import ast
    import inspect

    tree = ast.parse(
        inspect.getsource(interview_service.run_interview_schedule).lstrip()
    )
    loops = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFor | ast.For)]
    tenant_loop = loops[0]
    guarded = {
        ast.unparse(n)
        for stmt in tenant_loop.body
        if isinstance(stmt, ast.Try)
        for n in ast.walk(stmt)
    }
    assert any("get_settings_for_display" in g for g in guarded)
    assert any("period_hours" in g for g in guarded)
