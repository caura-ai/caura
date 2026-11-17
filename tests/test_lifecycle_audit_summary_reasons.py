"""The lifecycle audit summary counts failures by reason.

caura-enterprise's deploy gate reads this summary, and it used to carry only
counts by status. A nightly insights run that no model provider answered (L-134)
looked the same there as a broken pipeline, so one Vertex 429 on 17 Nov failed a
production promotion. A terminal failure that names its reason keeps it in
``stats.reason``, and the summary counts each action's failures by it in
``failure_reasons``. Rows are seeded with committed INSERTs on an independent
session, like the other storage tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from common.models import LifecycleAudit
from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_svc = PostgresService()


async def _row(org_id: str, *, action: str, status: str, stats: dict | None) -> None:
    finished = datetime.now(UTC)
    async with get_session() as s:
        s.add(
            LifecycleAudit(
                org_id=org_id,
                action=action,
                triggered_by="test",
                started_at=finished - timedelta(minutes=5),
                finished_at=finished,
                status=status,
                stats=stats,
                error_message=None if status == "success" else "failed",
            )
        )


async def test_failures_are_counted_by_the_reason_they_name():
    org = f"test-org-reasons-{uuid4().hex[:8]}"
    for _ in range(3):
        await _row(org, action="insights", status="success", stats={"insights": 1})
    await _row(
        org,
        action="insights",
        status="success",
        stats={"skipped": True, "reason": "recent_success"},
    )
    await _row(
        org,
        action="insights",
        status="failure",
        stats={"terminal": True, "reason": "llm_unavailable"},
    )
    await _row(org, action="insights", status="failure", stats={"terminal": True})
    await _row(org, action="insights", status="failure", stats=None)
    await _row(
        org, action="crystallize", status="success", stats={"links_or_clusters": 2}
    )

    summary = await _svc.lifecycle_audit_summary(org_id=org, since_hours=1)

    insights = summary["actions"]["insights"]
    assert insights["total"] == 7
    assert insights["statuses"] == {"success": 4, "failure": 3}
    # Only failures are counted by reason. A skip's "recent_success" is the reason
    # for a success, and a failure that names none isn't counted here at all.
    assert insights["failure_reasons"] == {"llm_unavailable": 1}
    assert summary["actions"]["crystallize"]["failure_reasons"] == {}
