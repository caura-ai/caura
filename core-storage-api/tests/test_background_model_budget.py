"""Tenant attempt reservations share one atomic authority across service instances."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from core_storage_api.config import settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
_PATH = "/api/v1/storage/background-work/reserve"


@pytest.fixture(autouse=True)
def cap(monkeypatch):
    # The old Settings lacks this field; an instance-dict override makes red
    # failures about the missing behavior rather than test setup/collection.
    monkeypatch.setitem(settings.__dict__, "background_llm_attempts_per_minute", 2)


async def _reserve(client, tenant, *, attempt=None, operation="entity-extraction", **extra):
    response = await client.post(
        _PATH,
        json={
            "tenant_id": tenant,
            "attempt_id": attempt or str(uuid4()),
            "operation": operation,
            **extra,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_a_tenant_exhausts_one_shared_budget_across_operations(client):
    tenant = f"budget-{uuid4()}"
    decisions = [
        await _reserve(client, tenant, operation=op)
        for op in (
            "entity-extraction",
            "enrichment",
            "contradiction",
            "interview",
        )
    ]
    assert [d["allowed"] for d in decisions] == [True, True, False, False]
    assert 0 < decisions[-1]["retry_after_seconds"] <= 60


async def test_a_lost_reservation_reply_can_be_retried_without_another_charge(client):
    tenant, attempt = f"budget-{uuid4()}", str(uuid4())
    first = await _reserve(client, tenant, attempt=attempt)
    replay = await _reserve(client, tenant, attempt=attempt)
    second = await _reserve(client, tenant)
    exhausted = await _reserve(client, tenant)
    assert first == replay
    assert [first["allowed"], second["allowed"], exhausted["allowed"]] == [True, True, False]


async def test_attempt_ids_are_scoped_to_the_tenant(client):
    first, other, attempt = f"budget-{uuid4()}", f"budget-{uuid4()}", str(uuid4())
    await _reserve(client, first)
    await _reserve(client, first)
    assert not (await _reserve(client, first, attempt=attempt))["allowed"]
    assert (await _reserve(client, other, attempt=attempt))["allowed"]


async def test_concurrent_callers_from_two_instances_cannot_overspend(client, monkeypatch):
    from core_storage_api.app import create_app

    monkeypatch.setitem(settings.__dict__, "background_llm_attempts_per_minute", 3)
    tenant = f"budget-{uuid4()}"
    # A fresh ASGI app has independent process-local state. Both apps' storage
    # transactions must nevertheless consume the same persisted tenant budget.
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
        headers=client.headers,
    ) as second:
        decisions = await asyncio.gather(*(_reserve(client if i % 2 else second, tenant) for i in range(24)))
    assert sum(d["allowed"] for d in decisions) == 3


async def test_a_new_app_does_not_reset_a_tenants_allowance(client):
    from core_storage_api.app import create_app

    tenant = f"budget-{uuid4()}"
    await _reserve(client, tenant)
    await _reserve(client, tenant)
    async with AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="http://test",
        headers=client.headers,
    ) as restarted:
        assert not (await _reserve(restarted, tenant))["allowed"]


@pytest.mark.parametrize("tenant", [None, "", " "])
async def test_missing_tenant_is_refused_before_reservation(client, tenant):
    response = await client.post(
        _PATH,
        json={
            "tenant_id": tenant,
            "attempt_id": str(uuid4()),
            "operation": "contradiction",
        },
    )
    assert response.status_code == 422, response.text


async def test_reservations_require_internal_storage_authentication(client):
    response = await client.post(
        _PATH,
        headers={"X-Storage-Secret": "wrong-synthetic-secret"},
        json={
            "tenant_id": f"budget-{uuid4()}",
            "attempt_id": str(uuid4()),
            "operation": "interview",
        },
    )
    assert response.status_code == 401, response.text
