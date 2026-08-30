"""Integration coverage for lifecycle-audit observation endpoints."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete

from common.models import LifecycleAudit
from core_storage_api.services.postgres_service import get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PREFIX = "/api/v1/storage/lifecycle-audit"


@pytest.fixture
async def lifecycle_marker() -> str:
    marker = f"smoke-test:{uuid.uuid4().hex}"
    yield marker
    async with get_session() as session:
        await session.execute(
            delete(LifecycleAudit).where(LifecycleAudit.triggered_by.in_((marker, f"{marker}:noise")))
        )


async def test_exact_read_and_uncapped_summary(
    client: AsyncClient,
    lifecycle_marker: str,
) -> None:
    marker = lifecycle_marker
    archive_ids: list[int] = []

    # Create the failure first, then cross the old diagnostic query's 30-row
    # cap. A newest-first capped listing would discard this row and go green.
    response = await client.post(
        PREFIX,
        json={
            "org_id": "test-tenant-lifecycle-observation",
            "action": "entity-link",
            "triggered_by": marker,
        },
    )
    assert response.status_code == 200, response.text
    entity_link_id = response.json()["audit_id"]
    failure = await client.patch(
        f"{PREFIX}/{entity_link_id}",
        json={"status": "failure", "error_message": "synthetic failure"},
    )
    assert failure.status_code == 200, failure.text

    for _ in range(31):
        response = await client.post(
            PREFIX,
            json={
                "org_id": "test-tenant-lifecycle-observation",
                "action": "archive-expired",
                "triggered_by": marker,
            },
        )
        assert response.status_code == 200, response.text
        archive_ids.append(response.json()["audit_id"])

    # Same action inside the time window but under a different trigger. If the
    # SQL drops the requested filter, this failure changes the expected count
    # and status map deterministically even on an otherwise empty test DB.
    response = await client.post(
        PREFIX,
        json={
            "org_id": "test-tenant-lifecycle-observation",
            "action": "archive-expired",
            "triggered_by": f"{marker}:noise",
        },
    )
    assert response.status_code == 200, response.text
    noise_id = response.json()["audit_id"]
    noise_failure = await client.patch(
        f"{PREFIX}/{noise_id}",
        json={"status": "failure", "error_message": "filter sentinel"},
    )
    assert noise_failure.status_code == 200, noise_failure.text

    for audit_id in archive_ids:
        success = await client.patch(
            f"{PREFIX}/{audit_id}",
            json={"status": "success", "stats": {"archived": 0}},
        )
        assert success.status_code == 200, success.text

    exact = await client.get(
        f"{PREFIX}/{archive_ids[0]}",
        params={"org_id": "test-tenant-lifecycle-observation"},
    )
    assert exact.status_code == 200, exact.text
    assert exact.json() == {
        "audit_id": archive_ids[0],
        "org_id": "test-tenant-lifecycle-observation",
        "action": "archive-expired",
        "triggered_by": marker,
        "started_at": exact.json()["started_at"],
        "finished_at": exact.json()["finished_at"],
        "status": "success",
        "stats": {"archived": 0},
        "error_message": None,
    }
    assert exact.json()["finished_at"] is not None

    wrong_org = await client.get(
        f"{PREFIX}/{archive_ids[0]}",
        params={"org_id": "a-different-tenant"},
    )
    assert wrong_org.status_code == 404

    summary = await client.post(
        f"{PREFIX}/summary",
        json={"org_id": None, "since_hours": 1, "triggered_by": marker},
    )
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["org_id"] is None
    assert body["since_hours"] == 1
    assert body["triggered_by"] == marker
    assert body["actions"]["archive-expired"]["total"] == 31
    assert body["actions"]["archive-expired"]["statuses"] == {"success": 31}
    assert body["actions"]["entity-link"]["total"] == 1
    assert body["actions"]["entity-link"]["statuses"] == {"failure": 1}

    response = await client.post(
        PREFIX,
        json={
            "org_id": "another-lifecycle-observation-tenant",
            "action": "archive-expired",
            "triggered_by": marker,
        },
    )
    assert response.status_code == 200, response.text
    other_org_id = response.json()["audit_id"]
    success = await client.patch(
        f"{PREFIX}/{other_org_id}",
        json={"status": "success", "stats": {"archived": 0}},
    )
    assert success.status_code == 200, success.text

    scoped = await client.post(
        f"{PREFIX}/summary",
        json={
            "org_id": "test-tenant-lifecycle-observation",
            "since_hours": 1,
            "triggered_by": marker,
        },
    )
    assert scoped.status_code == 200, scoped.text
    scoped_body = scoped.json()
    assert scoped_body["org_id"] == "test-tenant-lifecycle-observation"
    assert scoped_body["actions"]["archive-expired"]["total"] == 31
    assert scoped_body["actions"]["entity-link"]["total"] == 1


async def test_exact_read_returns_404_for_unknown_id(client: AsyncClient) -> None:
    response = await client.get(
        f"{PREFIX}/9223372036854775807",
        params={"org_id": "test-tenant-lifecycle-observation"},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("since_hours", [0, 169])
async def test_summary_rejects_unbounded_window(
    client: AsyncClient,
    since_hours: int,
) -> None:
    response = await client.post(
        f"{PREFIX}/summary",
        json={"org_id": None, "since_hours": since_hours},
    )
    assert response.status_code == 422


async def test_summary_requires_explicit_scope_choice(client: AsyncClient) -> None:
    response = await client.post(f"{PREFIX}/summary", json={"since_hours": 1})
    assert response.status_code == 422


@pytest.mark.parametrize("body", [[], 42])
async def test_summary_rejects_non_object_body(
    client: AsyncClient,
    body: object,
) -> None:
    response = await client.post(f"{PREFIX}/summary", json=body)
    assert response.status_code == 422
