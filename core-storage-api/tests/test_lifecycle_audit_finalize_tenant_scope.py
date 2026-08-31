"""Org scoping for lifecycle-audit status updates."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import delete

from common.models import LifecycleAudit
from core_storage_api.services.postgres_service import get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PREFIX = "/api/v1/storage/lifecycle-audit"


def _new_org_id() -> str:
    """Return a unique org id that matches the end-of-run sweep prefix."""
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def lifecycle_marker() -> str:
    marker = f"tenant-scope:{uuid.uuid4().hex}"
    yield marker
    async with get_session() as session:
        await session.execute(delete(LifecycleAudit).where(LifecycleAudit.triggered_by == marker))


async def _audit(client: AsyncClient, org_id: str, marker: str) -> int:
    response = await client.post(
        PREFIX,
        json={
            "org_id": org_id,
            "action": "archive-expired",
            "triggered_by": marker,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["audit_id"]


async def _row(client: AsyncClient, audit_id: int, org_id: str) -> dict:
    response = await client.get(f"{PREFIX}/{audit_id}", params={"org_id": org_id})
    assert response.status_code == 200, response.text
    return response.json()


class TestLifecycleAuditFinalizeTenantScope:
    async def test_route_does_not_finalize_another_orgs_audit(
        self,
        client: AsyncClient,
        lifecycle_marker: str,
    ) -> None:
        victim = _new_org_id()
        attacker = _new_org_id()
        audit_id = await _audit(client, victim, lifecycle_marker)

        response = await client.patch(
            f"{PREFIX}/{audit_id}",
            json={"org_id": attacker, "status": "success", "stats": {"archived": 9}},
        )

        assert response.status_code == 404, response.text
        row = await _row(client, audit_id, victim)
        assert row["status"] == "pending"
        assert row["stats"] is None

    async def test_route_requires_org_id(self, client: AsyncClient) -> None:
        response = await client.patch(f"{PREFIX}/1", json={"status": "success"})

        assert response.status_code == 422, response.text
        assert response.json()["detail"] == "'org_id' must be a non-empty string"

    async def test_route_passes_org_to_finalize_and_preserves_sticky_success(
        self,
        client: AsyncClient,
        lifecycle_marker: str,
    ) -> None:
        org_id = _new_org_id()
        audit_id = await _audit(client, org_id, lifecycle_marker)

        response = await client.patch(
            f"{PREFIX}/{audit_id}",
            json={"org_id": org_id, "status": "success", "stats": {"archived": 3}},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "noop": False}

        redelivery = await client.patch(
            f"{PREFIX}/{audit_id}",
            json={"org_id": org_id, "status": "failure", "error_message": "late"},
        )
        assert redelivery.status_code == 200, redelivery.text
        assert redelivery.json() == {"ok": True, "noop": True}

        row = await _row(client, audit_id, org_id)
        assert row["status"] == "success"
        assert row["stats"] == {"archived": 3}
        assert row["error_message"] is None
