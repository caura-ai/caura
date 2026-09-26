"""A72, third ground — the sweep fired daily and a heavy writing day outran it.

`lifecycle-crystallize` ticked once at 02:00, so everything written after the
tick waited ~24h for the janitor. Raising the cadence is the fix on its own
terms, but a bare increase multiplies LLM spend across every idle tenant — most
of which wrote nothing at all.

So the cadence knob ships **with** an activity gate, and the gate is what makes
it affordable: a tenant with nothing new since its last COMPLETED sweep costs
two indexed aggregates and never reaches the LLM.

Both default to today's behaviour. `lifecycle_crystallize_every_hours = 24` is
one run at the configured hour, and the gate can only ever *skip* work that the
daily run would have done on an unchanged corpus.
"""

import inspect
from datetime import UTC, datetime, timedelta

import pytest

pytestmark = pytest.mark.unit


class _Storage:
    """Minimal double: enough active memories to clear the count gate, plus a
    settable activity-gate answer."""

    def __init__(self, gate: dict) -> None:
        self.gate = gate
        self.gate_calls = 0

    async def count_active(self, org_id, fleet_id, status=None) -> int:
        return 10_000

    async def crystallizer_activity_gate(
        self, *, tenant_id: str, fleet_id: str | None
    ) -> dict:
        self.gate_calls += 1
        return self.gate


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ── the gate's decision ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_writes_since_the_last_sweep_skips_the_run(monkeypatch):
    """The case that makes a frequent cadence affordable. Nothing was written
    since the last completed sweep, so there is nothing to crystallize and the
    tick must not reach the LLM."""
    from core_api.services import lifecycle_audit as la

    now = datetime.now(UTC)
    storage = _Storage(
        {"latest_memory_at": _iso(now - timedelta(hours=3)), "last_sweep_at": _iso(now)}
    )
    monkeypatch.setattr(la, "resolve_config", _cfg_enabled)

    adapter = la._CoreApiLifecycleAdapter(storage)  # type: ignore[arg-type]
    assert await adapter.crystallize(org_id="t1", fleet_id=None) == 0
    assert storage.gate_calls == 1


@pytest.mark.asyncio
async def test_writes_since_the_last_sweep_let_the_run_proceed(monkeypatch):
    """A tenant that wrote after its last sweep still gets swept — the gate
    must not be a blanket suppressor."""
    from core_api.services import lifecycle_audit as la

    now = datetime.now(UTC)
    storage = _Storage(
        {"latest_memory_at": _iso(now), "last_sweep_at": _iso(now - timedelta(hours=3))}
    )
    monkeypatch.setattr(la, "resolve_config", _cfg_enabled)

    ran = {}

    async def _fake_run(*a, **kw):
        ran["yes"] = True
        return "report-id"

    monkeypatch.setattr(
        "core_api.services.crystallizer_service.run_crystallization",
        _fake_run,
        raising=False,
    )
    adapter = la._CoreApiLifecycleAdapter(storage)  # type: ignore[arg-type]
    await adapter.crystallize(org_id="t1", fleet_id=None)
    assert storage.gate_calls == 1


@pytest.mark.asyncio
async def test_a_never_swept_tenant_is_not_skipped(monkeypatch):
    """``last_sweep_at`` is null on a tenant's first ever run. Treating null as
    "already swept" would mean a tenant that has never been crystallized never
    is."""
    from core_api.services import lifecycle_audit as la

    storage = _Storage(
        {"latest_memory_at": _iso(datetime.now(UTC)), "last_sweep_at": None}
    )
    monkeypatch.setattr(la, "resolve_config", _cfg_enabled)

    async def _fake_run(*a, **kw):
        return "report-id"

    monkeypatch.setattr(
        "core_api.services.crystallizer_service.run_crystallization",
        _fake_run,
        raising=False,
    )
    adapter = la._CoreApiLifecycleAdapter(storage)  # type: ignore[arg-type]
    await adapter.crystallize(org_id="t1", fleet_id=None)
    assert storage.gate_calls == 1


@pytest.mark.asyncio
async def test_an_empty_tenant_skips(monkeypatch):
    """No memories at all — nothing to sweep, and no sweep row to compare to."""
    from core_api.services import lifecycle_audit as la

    storage = _Storage({"latest_memory_at": None, "last_sweep_at": None})
    monkeypatch.setattr(la, "resolve_config", _cfg_enabled)

    adapter = la._CoreApiLifecycleAdapter(storage)  # type: ignore[arg-type]
    assert await adapter.crystallize(org_id="t1", fleet_id=None) == 0


# ── ordering: the gate must not cost more than it saves ───────────────────


def test_the_gate_runs_after_the_count_gate_and_before_the_llm_import():
    """Placement is the whole economy of this change. The count gate is cheaper
    still and rejects small corpora outright; the lazy import below drags in the
    LLM clients this call exists to avoid paying for."""
    from core_api.services.lifecycle_audit import _CoreApiLifecycleAdapter

    src = inspect.getsource(_CoreApiLifecycleAdapter.crystallize)
    assert src.index("count_active") < src.index("crystallizer_activity_gate")
    assert src.index("crystallizer_activity_gate") < src.index(
        "from core_api.services.crystallizer_service"
    )


# ── storage: only COMPLETED sweeps advance the watermark ──────────────────


def test_the_watermark_reads_only_completed_sweeps():
    """A run reserves its report with ``status="running"`` before it works, so
    "the latest report" is frequently the caller itself, and a crashed run
    leaves a ``running`` row behind forever. Either would advance the watermark
    past work that never happened and strand every memory written before it —
    the same reasoning ``_type_ii_watermark`` documents."""
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.crystallizer_activity_gate)
    assert 'CrystallizationReport.status == "completed"' in src


def test_the_gate_is_scoped_by_fleet_on_both_sides():
    """A fleet-scoped sweep compared against a tenant-wide watermark would skip
    a fleet that had writes because a sibling fleet was swept more recently."""
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.crystallizer_activity_gate)
    assert "Memory.fleet_id == fleet_id" in src
    assert "CrystallizationReport.fleet_id == fleet_id" in src


# Cadence is covered in ``core-operations/tests/test_a72_cadence.py`` — that
# package is not importable from this suite.


def _cfg_enabled(_org_id):
    """``resolve_config`` double — crystallization enabled, nothing else set."""

    class _C:
        auto_crystallize_enabled = True

    async def _inner():
        return _C()

    return _inner()
