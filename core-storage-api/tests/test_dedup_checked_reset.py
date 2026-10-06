"""M-38 — clearing the crystallizer's dedup stamp returns rows to the sweep.

Against real Postgres, because the claim is about which rows the sweep's
candidate predicate (``last_dedup_checked_at IS NULL``) reaches afterwards.

``memory_mark_dedup_checked`` was the only writer of the stamp, and it only ever
set it, so a row settled under one crystallizer policy stayed out of the sweep
under every later one. The reset is what a settings change calls.

Each call clears one bounded batch, so no request outlives its client's timeout
however large the tenant; the route says whether rows may remain, and core-api
repeats the call until they do not.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient

from core_storage_api.routers import memories as memories_router
from core_storage_api.services.postgres_service import PostgresService
from tests.test_integration import PREFIX

# No blanket ``pytest.mark.asyncio``: ``asyncio_mode = auto`` covers these.

_VEC = [0.1] * 1024


def _tenant() -> str:
    """The prefix the root suite's sweep reclaims by."""
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


async def _stamped_memory(svc: PostgresService, tenant: str):
    memory = await svc.memory_add(
        {
            "tenant_id": tenant,
            "agent_id": "dedup-reset-tester",
            "content": f"dedup reset canary {uuid.uuid4()}",
            "memory_type": "fact",
            "weight": 0.5,
            "status": "active",
            "visibility": "scope_team",
            "embedding": _VEC,
        }
    )
    await svc.memory_mark_dedup_checked([memory.id], tenant)
    return memory


async def _candidates(svc: PostgresService, tenant: str) -> set:
    rows = await svc.memory_find_near_duplicate_pairs(tenant_id=tenant, fleet_id=None, batch_size=50)
    return {r[0] for r in rows}


async def test_a_reset_returns_stamped_rows_to_the_sweep():
    svc = PostgresService()
    tenant = _tenant()
    memory = await _stamped_memory(svc, tenant)
    assert memory.id not in await _candidates(svc, tenant), "the stamp should hold it out"

    assert await svc.memory_reset_dedup_checked(tenant, limit=10) == 1

    assert memory.id in await _candidates(svc, tenant)


async def test_one_reset_clears_at_most_one_batch():
    svc = PostgresService()
    tenant = _tenant()
    written = {(await _stamped_memory(svc, tenant)).id for _ in range(5)}

    assert await svc.memory_reset_dedup_checked(tenant, limit=2) == 2
    assert len(written & await _candidates(svc, tenant)) == 2


async def test_repeated_resets_drain_the_tenant():
    svc = PostgresService()
    tenant = _tenant()
    written = {(await _stamped_memory(svc, tenant)).id for _ in range(5)}

    cleared = [await svc.memory_reset_dedup_checked(tenant, limit=2) for _ in range(4)]

    assert cleared == [2, 2, 1, 0]
    assert written <= await _candidates(svc, tenant)


async def test_a_reset_is_bounded_to_its_tenant():
    svc = PostgresService()
    mine, theirs = _tenant(), _tenant()
    await _stamped_memory(svc, mine)
    other = await _stamped_memory(svc, theirs)

    await svc.memory_reset_dedup_checked(mine, limit=10)

    assert other.id not in await _candidates(svc, theirs)


async def test_the_reset_route_requires_a_tenant(client: AsyncClient):
    resp = await client.post(f"{PREFIX}/memories/reset-dedup-checked", json={})
    assert resp.status_code == 422, resp.text


async def test_the_reset_route_reports_the_count(client: AsyncClient):
    svc = PostgresService()
    tenant = _tenant()
    await _stamped_memory(svc, tenant)

    resp = await client.post(f"{PREFIX}/memories/reset-dedup-checked", json={"tenant_id": tenant})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"reset": 1, "done": True}


async def test_the_reset_route_says_when_rows_may_remain(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(memories_router, "_DEDUP_RESET_BATCH", 1, raising=False)
    svc = PostgresService()
    tenant = _tenant()
    for _ in range(2):
        await _stamped_memory(svc, tenant)

    resp = await client.post(f"{PREFIX}/memories/reset-dedup-checked", json={"tenant_id": tenant})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"reset": 1, "done": False}
