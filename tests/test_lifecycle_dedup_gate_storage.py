"""The lifecycle dedup gate counts real runs only.

A skipped delivery is finalized as ``success`` with ``stats.skipped=true``.
The gate used to count those rows too, so once any cadence under the window
skipped once, every later tick saw a "recent success" and skipped again,
forever. Rows are seeded with committed INSERTs on an independent session,
like the other storage tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from common.models import LifecycleAudit
from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_svc = PostgresService()


async def _row(
    org_id: str,
    *,
    hours_ago: float,
    stats: dict | None,
    started_hours_ago: float | None = None,
) -> None:
    finished = datetime.now(UTC) - timedelta(hours=hours_ago)
    started = (
        finished
        if started_hours_ago is None
        else datetime.now(UTC) - timedelta(hours=started_hours_ago)
    )
    async with get_session() as s:
        s.add(
            LifecycleAudit(
                org_id=org_id,
                action="crystallize",
                triggered_by="test",
                started_at=started,
                finished_at=finished,
                status="success",
                stats=stats,
            )
        )


async def _recent(org_id: str, hours: float) -> bool:
    return await _svc.lifecycle_audit_has_recent_success(
        org_id=org_id, action="crystallize", since_hours=hours
    )


async def test_a_skip_row_does_not_count_as_a_recent_success():
    org = f"test-org-dedup-{uuid4().hex[:8]}"
    await _row(org, hours_ago=1, stats={"skipped": True, "reason": "recent_success"})
    assert await _recent(org, 23) is False


async def test_a_real_run_still_counts():
    org = f"test-org-dedup-{uuid4().hex[:8]}"
    await _row(org, hours_ago=1, stats={"crystallized": 3})
    await _row(org, hours_ago=0.5, stats=None)
    assert await _recent(org, 23) is True


async def test_skip_chains_no_longer_lock_the_op_out():
    """The original failure: real run, then a skip every tick after it."""
    org = f"test-org-dedup-{uuid4().hex[:8]}"
    await _row(org, hours_ago=30, stats={"crystallized": 1})  # outside the window
    for h in (11, 10, 9, 1):
        await _row(
            org, hours_ago=h, stats={"skipped": True, "reason": "recent_success"}
        )
    assert await _recent(org, 23) is False


async def test_fractional_windows_work():
    org = f"test-org-dedup-{uuid4().hex[:8]}"
    await _row(org, hours_ago=0.9, stats={"crystallized": 1})  # previous hourly run
    assert await _recent(org, 0.5) is False
    assert await _recent(org, 1.0) is True


async def test_a_run_that_finished_late_does_not_suppress_the_next_tick():
    """The window belongs to the tick, not to when its run happened to finish.

    The reconcile sweep only republishes rows older than 30 minutes and runs at
    half past, so a 02:00 row whose publish was lost runs at 03:30. Measured
    from ``finished_at`` that success sat inside the next 02:00 tick's 23h
    window and the org skipped a night; a long run did the same.
    """
    org = f"test-org-dedup-{uuid4().hex[:8]}"
    # Yesterday's 02:00 row (24h ago), repaired and finished at ~03:31.
    await _row(org, started_hours_ago=24, hours_ago=22.5, stats={"crystallized": 2})
    assert await _recent(org, 23) is False


async def test_a_double_fired_tick_is_still_deduplicated():
    org = f"test-org-dedup-{uuid4().hex[:8]}"
    await _row(org, started_hours_ago=0.5, hours_ago=0.2, stats={"crystallized": 2})
    assert await _recent(org, 23) is True
