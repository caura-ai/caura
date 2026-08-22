"""``report_find_running`` must not let an orphaned row wedge crystallization.

OSS #817. ``run_crystallization`` short-circuits on whatever this returns, so a
row left in ``status='running'`` by a crashed run disabled crystallization for
that tenant — for every trigger, forever, until someone edited the row by hand.

Two defects, tested separately:

* no staleness cutoff, so an orphaned row matched indefinitely;
* ``scalar_one_or_none()``, which raises ``MultipleResultsFound`` when two rows
  match — reachable because two racing first calls each create one — turning the
  wedge into a 500 on every subsequent call.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from common.constants import REPORT_RUNNING_STALE_AFTER
from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _insert_report(*, tenant_id, fleet_id, status, started_at):
    """Insert an analysis_reports row directly.

    Direct SQL because the point is to create states the service will not: an
    orphaned 'running' row, and two of them at once.
    """
    report_id = uuid.uuid4()
    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO analysis_reports
                    (id, tenant_id, fleet_id, trigger, status, started_at)
                VALUES
                    (:id, :tenant_id, :fleet_id, 'scheduled', :status, :started_at)
                """
            ),
            {
                "id": report_id,
                "tenant_id": tenant_id,
                "fleet_id": fleet_id,
                "status": status,
                "started_at": started_at,
            },
        )
    return report_id


async def test_a_fresh_running_report_is_still_returned(_ensure_schema) -> None:
    """The lock must keep working. Fixing the wedge by disabling the
    short-circuit would let two runs overlap on every trigger."""
    tenant = f"t-fresh-{uuid.uuid4().hex[:8]}"
    report_id = await _insert_report(
        tenant_id=tenant,
        fleet_id=None,
        status="running",
        started_at=datetime.now(UTC),
    )

    found = await PostgresService().report_find_running(tenant, None)

    assert found == report_id


async def test_a_stale_running_report_no_longer_blocks(_ensure_schema) -> None:
    """The wedge. Before the cutoff this row matched forever, so every later run
    returned its id and did nothing."""
    tenant = f"t-stale-{uuid.uuid4().hex[:8]}"
    await _insert_report(
        tenant_id=tenant,
        fleet_id=None,
        status="running",
        # One minute past the ceiling — just stale, so the test pins the boundary
        # rather than passing on an obviously ancient row.
        started_at=datetime.now(UTC) - REPORT_RUNNING_STALE_AFTER - timedelta(minutes=1),
    )

    found = await PostgresService().report_find_running(tenant, None)

    assert found is None, (
        "an orphaned report is still treated as in flight, so crystallization stays disabled for this tenant"
    )


async def test_a_report_just_inside_the_cutoff_still_blocks(_ensure_schema) -> None:
    """The other side of the boundary: a long-but-live run must keep its lock, or
    the cutoff would start cancelling real work by letting a second run in."""
    tenant = f"t-inside-{uuid.uuid4().hex[:8]}"
    report_id = await _insert_report(
        tenant_id=tenant,
        fleet_id=None,
        status="running",
        started_at=datetime.now(UTC) - REPORT_RUNNING_STALE_AFTER + timedelta(minutes=1),
    )

    found = await PostgresService().report_find_running(tenant, None)

    assert found == report_id


async def test_two_running_reports_return_one_instead_of_raising(_ensure_schema) -> None:
    """H-04 family. ``scalar_one_or_none()`` raises ``MultipleResultsFound`` on
    two matching rows, which is a 500 on EVERY call rather than a wedge — and two
    rows is reachable, because two racing first calls each create one and nothing
    in the schema prevents it.

    Newest wins: this answers "is a run in flight", and the newest is the one most
    likely still alive.
    """
    tenant = f"t-two-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)
    await _insert_report(
        tenant_id=tenant, fleet_id=None, status="running", started_at=now - timedelta(minutes=10)
    )
    newer = await _insert_report(
        tenant_id=tenant, fleet_id=None, status="running", started_at=now - timedelta(minutes=1)
    )

    found = await PostgresService().report_find_running(tenant, None)

    assert found == newer, f"expected the newest running report, got {found}"


async def test_a_completed_report_never_blocks(_ensure_schema) -> None:
    """The guard against a cutoff that accidentally widened the match."""
    tenant = f"t-done-{uuid.uuid4().hex[:8]}"
    await _insert_report(tenant_id=tenant, fleet_id=None, status="completed", started_at=datetime.now(UTC))

    assert await PostgresService().report_find_running(tenant, None) is None


async def test_another_fleet_s_running_report_does_not_block(_ensure_schema) -> None:
    """Scope. A fleet's run must not hold a lock over a sibling fleet."""
    tenant = f"t-fleet-{uuid.uuid4().hex[:8]}"
    await _insert_report(tenant_id=tenant, fleet_id="fleet-a", status="running", started_at=datetime.now(UTC))

    assert await PostgresService().report_find_running(tenant, "fleet-b") is None
    assert await PostgresService().report_find_running(tenant, None) is None
