"""``PATCH /reports/{report_id}`` must be told which tenant's report it completes.

The write form of GHSA-wgvw-28pq-jc36 on crystallization reports. The route took
a bare UUID and ``report_update_completed`` matched on the primary key alone, so
a caller who knew an id could finalize another tenant's report through a service
that authenticates nothing.

This path writes more than most: ``status``, ``completed_at``, ``duration_ms``
and six JSONB blobs --- ``summary``, ``hygiene``, ``health``, ``usage_data``,
``issues``, ``crystallization`` --- every one of them straight from the body. So
the reachable outcome is substitution rather than disclosure: a victim's report
could be marked ``completed`` carrying an attacker's scores and issue list, and
the tenant reading their own dashboard would see exactly that. It is the
data-integrity half the issue calls out, and it is why this one mattered more
than its read-side sibling.

Marking a *running* report ``completed`` has a second effect, because
``report_find_running`` is the short-circuit that serialises crystallization: it
matches ``status == "running"``, so flipping that column out from under a live
run releases the lock and lets a concurrent run start.

``tenant_id`` is now required --- 422 when absent, rather than falling back to
"update by primary key" --- and is part of the UPDATE predicate, so another
tenant's report is indistinguishable from one that does not exist.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = [pytest.mark.asyncio]


async def _report(client: AsyncClient, tenant_id: str) -> str:
    resp = await client.post(
        f"{PREFIX}/reports",
        json={"tenant_id": tenant_id, "trigger": "manual"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _read(client: AsyncClient, report_id: str) -> dict:
    """Read the row back regardless of tenant, to assert what actually persisted.

    Reads ``crystallization_reports`` through the ORM rather than through
    ``GET /reports/{report_id}``, which it used to use. The note that stood here
    said this helper would need a tenant the day that route took one; it now
    does, and giving it one would have been the wrong repair --- the same
    conclusion ``test_entity_update_tenant_scope`` reached when its route was
    scoped. Every assertion below asks "is the row still in the tenant that
    owned it, holding the values it had", so an oracle scoped to a tenant can
    only answer about rows already known to be in it, and could not observe a
    row that moved namespace. That is precisely the failure
    ``test_another_tenants_report_is_not_completed`` exists to catch, and the
    last assertion in it (``row["tenant_id"] == victim``) is the one that would
    go silently untested. Tenant-blind by construction, and so immune to any
    further scoping of the route.

    ``client`` is unused but kept in the signature so the call sites read the
    same and the raw read stays swappable.
    """
    from common.models import CrystallizationReport
    from core_storage_api.services.postgres_service import get_session

    async with get_session() as session:
        report = await session.get(CrystallizationReport, uuid.UUID(report_id))
    assert report is not None, f"report {report_id} vanished"
    return {
        "id": str(report.id),
        "tenant_id": report.tenant_id,
        "status": report.status,
        "completed_at": report.completed_at,
        "duration_ms": report.duration_ms,
        "summary": report.summary,
        "hygiene": report.hygiene,
        "health": report.health,
        "usage_data": report.usage_data,
        "issues": report.issues,
        "crystallization": report.crystallization,
    }


def _completion(**extra: object) -> dict:
    """A well-formed completion body, minus whatever the caller overrides."""
    return {
        "status": "completed",
        "completed_at": datetime.now(UTC).isoformat(),
        "duration_ms": 1234,
        **extra,
    }


class TestReportUpdateTenantScope:
    async def test_another_tenants_report_is_not_completed(self, client: AsyncClient) -> None:
        """The attack, stated as the request an attacker would send."""
        victim = f"rep-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"rep-attacker-{uuid.uuid4().hex[:8]}"
        report_id = await _report(client, victim)

        resp = await client.patch(
            f"{PREFIX}/reports/{report_id}",
            json=_completion(
                tenant_id=attacker,
                summary={"overall_score": 3, "critical": 99},
                issues=[{"severity": "critical", "message": "OWNED"}],
                crystallization={"promoted": 0},
            ),
        )

        assert resp.status_code == 404, resp.text
        row = await _read(client, report_id)
        assert row["status"] == "running", "the victim's report was finalized"
        assert row["completed_at"] is None
        assert row["duration_ms"] is None
        # The substitution half: none of the caller-supplied content landed.
        assert row["summary"] == {}
        assert row["issues"] == []
        assert row["crystallization"] == {}
        # Naming a tenant you don't own re-points the predicate; it never moves
        # the row into the tenant you named.
        assert row["tenant_id"] == victim

    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        """No tenant used to mean "update by primary key"; it is now a 422.

        Fail-closed matters more here than on the read paths: a caller that
        simply forgot the field would otherwise get the old unscoped write and
        no indication anything was wrong.
        """
        tenant = f"rep-{uuid.uuid4().hex[:8]}"
        report_id = await _report(client, tenant)

        resp = await client.patch(f"{PREFIX}/reports/{report_id}", json=_completion())

        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.text
        assert (await _read(client, report_id))["status"] == "running"

    async def test_own_report_is_completed(self, client: AsyncClient) -> None:
        """The supported call still works, with every blob it is meant to write."""
        tenant = f"rep-{uuid.uuid4().hex[:8]}"
        report_id = await _report(client, tenant)

        resp = await client.patch(
            f"{PREFIX}/reports/{report_id}",
            json=_completion(
                tenant_id=tenant,
                summary={"overall_score": 88},
                hygiene={"orphans": 2},
                health={"ok": True},
                usage_data={"tokens": 41},
                issues=[{"severity": "warning", "message": "stale"}],
                crystallization={"promoted": 7},
            ),
        )

        assert resp.status_code == 200, resp.text
        row = await _read(client, report_id)
        assert row["status"] == "completed"
        assert row["duration_ms"] == 1234
        assert row["completed_at"] is not None
        assert row["summary"] == {"overall_score": 88}
        assert row["hygiene"] == {"orphans": 2}
        assert row["health"] == {"ok": True}
        assert row["usage_data"] == {"tokens": 41}
        assert row["issues"] == [{"severity": "warning", "message": "stale"}]
        assert row["crystallization"] == {"promoted": 7}
        assert row["tenant_id"] == tenant

    async def test_a_foreign_id_is_indistinguishable_from_a_missing_one(self, client: AsyncClient) -> None:
        """Both 404, so the endpoint is not an existence oracle for report UUIDs."""
        victim = f"rep-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"rep-attacker-{uuid.uuid4().hex[:8]}"
        report_id = await _report(client, victim)

        body = _completion(tenant_id=attacker)
        foreign = await client.patch(f"{PREFIX}/reports/{report_id}", json=body)
        missing = await client.patch(f"{PREFIX}/reports/{uuid.uuid4()}", json=body)

        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()

    async def test_the_crystallization_lock_is_not_released_by_another_tenant(
        self, client: AsyncClient
    ) -> None:
        """The availability half, pinned through the query that actually reads it.

        ``report_find_running`` is what ``run_crystallization`` consults to
        decide a run is already in flight. It matches on ``status``, so a
        cross-tenant flip to ``completed`` used to release that lock while the
        real run was still going.
        """
        victim = f"rep-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"rep-attacker-{uuid.uuid4().hex[:8]}"
        report_id = await _report(client, victim)

        running = await client.get(f"{PREFIX}/reports/running", params={"tenant_id": victim})
        assert running.status_code == 200, running.text
        assert running.json()["id"] == report_id, "precondition: the lock is held"

        resp = await client.patch(f"{PREFIX}/reports/{report_id}", json=_completion(tenant_id=attacker))

        assert resp.status_code == 404, resp.text
        still = await client.get(f"{PREFIX}/reports/running", params={"tenant_id": victim})
        assert still.status_code == 200, "the crystallization lock was released"
        assert still.json()["id"] == report_id
