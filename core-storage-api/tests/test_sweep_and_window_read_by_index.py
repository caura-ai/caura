"""The contradiction window and the expiry sweep are read by index (audit 2026-10-01, B33).

L-185: ``outcome_contradiction_signals`` windows on ``COALESCE(status_changed_at,
created_at)``, since 045 left the column unbackfilled. 045's index keyed the bare
column, which cannot serve a range over that expression, so the window was a
filter over every contradicted row of the tenant.

L-191: ``memory_archive_expired`` takes ``ts_valid_end < NOW() OR expires_at <
NOW()`` among a tenant's live rows. No index served either arm, so each daily run
read the tenant's whole live slice.

Each check plans the SQL the service method runs, with the parameters it binds,
captured from the session it opens. The parameters are written in as literals,
as a custom plan sees them, and the plan is made against an empty copy of
``memories`` that has only the indexes in question (``plan_with_only_index``).
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from common.constants import CONTRADICTED_STATUSES
from core_storage_api.services import postgres_service
from core_storage_api.services.postgres_service import PostgresService
from tests.conftest import plan_with_only_index

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("_ensure_schema")]


class _Captured:
    """A session that records what it is asked to run and returns no rows."""

    def __init__(self) -> None:
        self.statements: list[tuple] = []

    async def execute(self, statement, params=None):
        self.statements.append((statement, params or {}))
        return SimpleNamespace(all=lambda: [], fetchall=lambda: [])


@pytest.fixture
def captured(monkeypatch) -> _Captured:
    session = _Captured()

    @asynccontextmanager
    async def _open():
        yield session

    monkeypatch.setattr(postgres_service, "get_session", _open)
    monkeypatch.setattr(postgres_service, "get_read_session", _open)
    return session


def _literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, list | tuple):
        return "ARRAY[" + ", ".join(_literal(item) for item in value) + "]::text[]"
    if isinstance(value, datetime):
        return f"'{value.isoformat()}'::timestamptz"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _as_planned(statement, params: dict):
    """``statement`` with each bound parameter written in as a literal."""
    return text(re.sub(r"(?<![:\w]):(\w+)", lambda m: _literal(params[m.group(1)]), statement.text))


@pytest.mark.parametrize("fleet_id", [None, "f-plan"], ids=["tenant", "fleet"])
async def test_l185_the_contradiction_window_is_an_index_condition(captured, fleet_id) -> None:
    end = datetime.now(UTC)
    await PostgresService().outcome_contradiction_signals(
        tenant_id="t-plan",
        fleet_id=fleet_id,
        window_start=end - timedelta(days=1),
        window_end=end,
        contradicted_statuses=list(CONTRADICTED_STATUSES),
    )
    [(statement, params)] = captured.statements

    plan = await plan_with_only_index(_as_planned(statement, params), "ix_memories_contradicted_window")

    # An index on the bare column matched only the tenant, and the window was
    # left to a filter.
    conditions = [line for line in plan.splitlines() if "Index Cond" in line]
    assert any("COALESCE(status_changed_at, created_at)" in line for line in conditions), plan


@pytest.mark.parametrize("fleet_id", [None, "f-plan"], ids=["tenant", "fleet"])
async def test_l191_each_arm_of_the_expiry_sweep_is_read_by_index(captured, fleet_id) -> None:
    await PostgresService().memory_archive_expired(tenant_id="t-plan", fleet_id=fleet_id)
    [(statement, params)] = captured.statements

    plan = await plan_with_only_index(
        _as_planned(statement, params), "ix_memories_expires_at", "ix_memories_valid_end"
    )

    # One index per arm, ORed: neither arm on its own finds every expired row.
    assert "BitmapOr" in plan, plan
    assert "ix_memories_expires_at" in plan, plan
    assert "ix_memories_valid_end" in plan, plan
