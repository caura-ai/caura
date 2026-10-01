"""Pointer columns name rows of the writer's own tenant, or the write is refused.

``subject_entity_id``, ``supersedes_id`` and ``evidence_memory_id`` are foreign
keys with no tenant pairing. Every writer scoped the TARGET row to the caller's
tenant but never the row the pointer names, so an unknown UUID surfaced as an
unhandled ``ForeignKeyViolationError`` (500 — which core-api answers "503,
retry"), and another tenant's UUID was accepted and persisted a cross-tenant
edge. Both now get the same 422, marked not retryable, naming only the field.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from common import permanent_failure
from tests.test_integration import PREFIX, _memory_payload

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _tenant() -> str:
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


async def _memory(client: AsyncClient, tenant: str, **extra) -> str:
    payload = {**_memory_payload(tenant, "fleet-p"), **extra}
    resp = await client.post(f"{PREFIX}/memories", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _entity(client: AsyncClient, tenant: str) -> str:
    resp = await client.post(
        f"{PREFIX}/entities",
        json={"tenant_id": tenant, "entity_type": "person", "canonical_name": f"P-{uuid.uuid4().hex[:8]}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _assert_refused(resp, field: str) -> None:
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == permanent_failure.CAUSE_POINTER_NOT_IN_TENANT
    assert detail["retryable"] is False
    assert detail["field"] == field


async def _column(memory_id: str, column: str):
    from core_storage_api.services.postgres_service import get_session

    async with get_session() as session:
        return (
            await session.execute(
                text(f"SELECT {column} FROM memories WHERE id = CAST(:id AS uuid)"), {"id": memory_id}
            )
        ).scalar()


@pytest.fixture
async def tenants(client: AsyncClient) -> dict:
    mine, other = _tenant(), _tenant()
    return {
        "mine": mine,
        "own_entity": await _entity(client, mine),
        "own_memory": await _memory(client, mine),
        "foreign_entity": await _entity(client, other),
        "foreign_memory": await _memory(client, other),
    }


class TestPointerTenantScope:
    @pytest.mark.parametrize("which", ["foreign", "unknown"])
    async def test_create_refuses_a_subject_outside_the_tenant(
        self, client: AsyncClient, tenants: dict, which: str
    ) -> None:
        target = tenants["foreign_entity"] if which == "foreign" else str(uuid.uuid4())
        payload = {**_memory_payload(tenants["mine"], "fleet-p"), "subject_entity_id": target}
        _assert_refused(await client.post(f"{PREFIX}/memories", json=payload), "subject_entity_id")

    async def test_create_accepts_an_own_subject(self, client: AsyncClient, tenants: dict) -> None:
        mid = await _memory(client, tenants["mine"], subject_entity_id=tenants["own_entity"])
        assert str(await _column(mid, "subject_entity_id")) == tenants["own_entity"]

    async def test_bulk_refuses_the_batch(self, client: AsyncClient, tenants: dict) -> None:
        items = []
        for subject in (tenants["own_entity"], tenants["foreign_entity"]):
            item = _memory_payload(tenants["mine"], "fleet-p")
            item["subject_entity_id"] = subject
            item["client_request_id"] = str(uuid.uuid4())
            items.append(item)
        _assert_refused(await client.post(f"{PREFIX}/memories/bulk", json=items), "subject_entity_id")

    async def test_patch_refuses_a_foreign_subject(self, client: AsyncClient, tenants: dict) -> None:
        mid = tenants["own_memory"]
        resp = await client.patch(
            f"{PREFIX}/memories/{mid}",
            json={"tenant_id": tenants["mine"], "subject_entity_id": tenants["foreign_entity"]},
        )
        _assert_refused(resp, "subject_entity_id")
        assert await _column(mid, "subject_entity_id") is None

    async def test_subject_write_back_skips_a_foreign_entity(
        self, client: AsyncClient, tenants: dict
    ) -> None:
        """The CAS write-back answers "not written" — its answer for every other
        row it must not touch — instead of a 500 or a cross-tenant subject."""
        mid = tenants["own_memory"]
        resp = await client.post(
            f"{PREFIX}/memories/{mid}/subject-entity",
            json={"tenant_id": tenants["mine"], "subject_entity_id": tenants["foreign_entity"]},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"updated": False}
        assert await _column(mid, "subject_entity_id") is None

    @pytest.mark.parametrize("which", ["foreign", "unknown"])
    async def test_status_route_refuses_a_foreign_supersedes_before_flipping(
        self, client: AsyncClient, tenants: dict, which: str
    ) -> None:
        mid = tenants["own_memory"]
        target = tenants["foreign_memory"] if which == "foreign" else str(uuid.uuid4())
        resp = await client.patch(
            f"{PREFIX}/memories/{mid}/status",
            json={"tenant_id": tenants["mine"], "status": "outdated", "supersedes_id": target},
        )
        _assert_refused(resp, "supersedes_id")
        assert await _column(mid, "status") == "active"

    async def test_batch_status_refuses_before_writing_any_row(
        self, client: AsyncClient, tenants: dict
    ) -> None:
        first = await _memory(client, tenants["mine"])
        resp = await client.post(
            f"{PREFIX}/memories/batch-update-status",
            json={
                "tenant_id": tenants["mine"],
                "updates": [
                    {"memory_id": first, "status": "outdated", "supersedes_id": tenants["own_memory"]},
                    {
                        "memory_id": tenants["own_memory"],
                        "status": "outdated",
                        "supersedes_id": tenants["foreign_memory"],
                    },
                ],
            },
        )
        _assert_refused(resp, "supersedes_id")
        assert await _column(first, "status") == "active"

    async def test_relation_refuses_foreign_evidence(self, client: AsyncClient, tenants: dict) -> None:
        other_end = await _entity(client, tenants["mine"])
        body = {
            "tenant_id": tenants["mine"],
            "from_entity_id": tenants["own_entity"],
            "to_entity_id": other_end,
            "relation_type": "knows",
            "weight": 0.5,
        }
        _assert_refused(
            await client.post(
                f"{PREFIX}/entities/relations", json={**body, "evidence_memory_id": tenants["foreign_memory"]}
            ),
            "evidence_memory_id",
        )
        ok = await client.post(
            f"{PREFIX}/entities/relations", json={**body, "evidence_memory_id": tenants["own_memory"]}
        )
        assert ok.status_code == 200, ok.text
