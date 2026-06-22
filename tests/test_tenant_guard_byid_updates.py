"""Tenant-guard on by-id memory UPDATEs (CAURA-000).

Every by-id write that core-api routes through core-storage-api must be
scoped to the row's home tenant, so a wrong-tenant ``memory_id`` can never
mutate a foreign row. Covers the three storage ops hardened here:

- PATCH /memories/{id}                -> memory_update            (content PATCH)
- PATCH /memories/{id}/embedding      -> memory_update_embedding  (worker/backfill)
- POST  /memories/mark-dedup-checked  -> memory_mark_dedup_checked

Wrong-tenant calls must be no-ops; same-tenant calls apply. Rows are seeded
via the storage write path (committed, independent session) and asserted via
``PostgresService`` directly — mirrors tests/test_ph3_storage_documents_fleet.py.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest

from common.embedding import fake_embedding
from core_storage_api.services.postgres_service import PostgresService

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _t() -> str:
    """Unique tenant id per test so concurrent suite runs don't collide."""
    return f"test-tenant-guard-{uuid4().hex[:8]}"


async def _seed_memory(sc, tenant: str, *, embedding=None, metadata=None) -> str:
    payload: dict = {
        "tenant_id": tenant,
        "agent_id": "test-agent",
        "content": f"content {uuid4().hex[:8]}",
        "memory_type": "fact",
        "weight": 0.5,
        "status": "active",
        "visibility": "scope_team",
    }
    if embedding is not None:
        payload["embedding"] = embedding
    if metadata is not None:
        payload["metadata_"] = metadata
    row = await sc.create_memory(payload)
    return row["id"]


# ---------------------------------------------------------------------------
# memory_update — content PATCH (PATCH /memories/{id})
# ---------------------------------------------------------------------------


async def test_memory_update_wrong_tenant_is_noop(sc):
    svc = PostgresService()
    owner = _t()
    mem_id = UUID(await _seed_memory(sc, owner))

    applied = await svc.memory_update(mem_id, _t(), {"weight": 0.99})
    assert applied is False, "wrong-tenant update matches no row"
    row = await svc.memory_get_by_id(mem_id)
    assert row is not None and row.weight == 0.5, "weight untouched by wrong tenant"

    applied_owner = await svc.memory_update(mem_id, owner, {"weight": 0.99})
    assert applied_owner is True
    assert (await svc.memory_get_by_id(mem_id)).weight == 0.99


async def test_memory_update_metadata_patch_wrong_tenant_is_noop(sc):
    svc = PostgresService()
    owner = _t()
    mem_id = UUID(await _seed_memory(sc, owner, metadata={"k": "orig"}))

    await svc.memory_update(mem_id, _t(), {"metadata_patch": {"k": "hacked"}})
    assert (await svc.memory_get_by_id(mem_id)).metadata_ == {"k": "orig"}, (
        "metadata merge blocked for wrong tenant"
    )

    await svc.memory_update(mem_id, owner, {"metadata_patch": {"k": "new", "added": 1}})
    assert (await svc.memory_get_by_id(mem_id)).metadata_ == {"k": "new", "added": 1}


async def test_update_memory_route_wrong_tenant_returns_none(sc):
    # End-to-end through the typed client: wrong tenant -> route 404 -> None.
    owner = _t()
    mem_id = await _seed_memory(sc, owner)

    result = await sc.update_memory(mem_id, _t(), {"weight": 0.42})
    assert result is None, "wrong-tenant PATCH 404s -> client returns None"
    assert (await PostgresService().memory_get_by_id(UUID(mem_id))).weight == 0.5

    ok = await sc.update_memory(mem_id, owner, {"weight": 0.42})
    assert ok == {"ok": True}
    assert (await PostgresService().memory_get_by_id(UUID(mem_id))).weight == 0.42


async def test_update_memory_route_requires_tenant(sc):
    # The route's belt-and-suspenders guard: a raw PATCH with no tenant_id
    # is rejected 422 rather than running an unscoped UPDATE.
    mem_id = await _seed_memory(sc, _t())
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc._patch(f"/memories/{mem_id}", {"weight": 0.1})
    assert exc.value.response.status_code == 422


# ---------------------------------------------------------------------------
# memory_update_embedding (PATCH /memories/{id}/embedding)
# ---------------------------------------------------------------------------


async def test_update_embedding_wrong_tenant_is_noop(sc):
    svc = PostgresService()
    owner = _t()
    mem_id = await _seed_memory(sc, owner)  # seeded without an embedding
    assert (await svc.memory_get_by_id(UUID(mem_id))).embedding is None

    # Wrong tenant matches no row -> route 404 -> client returns None, no write.
    wrong = await sc.update_embedding(mem_id, _t(), fake_embedding("vector"))
    assert wrong is None, "wrong-tenant embedding write 404s -> client None"
    assert (await svc.memory_get_by_id(UUID(mem_id))).embedding is None, (
        "wrong tenant must not write the embedding"
    )

    ok = await sc.update_embedding(mem_id, owner, fake_embedding("vector"))
    assert ok == {"ok": True}
    assert (await svc.memory_get_by_id(UUID(mem_id))).embedding is not None


async def test_update_embedding_requires_tenant(sc):
    mem_id = await _seed_memory(sc, _t())
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc._patch(f"/memories/{mem_id}/embedding", {"embedding": fake_embedding("x")})
    assert exc.value.response.status_code == 422


# ---------------------------------------------------------------------------
# memory_mark_dedup_checked (POST /memories/mark-dedup-checked)
# ---------------------------------------------------------------------------


async def test_mark_dedup_checked_wrong_tenant_is_noop(sc):
    svc = PostgresService()
    owner = _t()
    mem_id = await _seed_memory(sc, owner)
    assert (await svc.memory_get_by_id(UUID(mem_id))).last_dedup_checked_at is None

    await sc.mark_dedup_checked([mem_id], _t())
    assert (await svc.memory_get_by_id(UUID(mem_id))).last_dedup_checked_at is None, (
        "wrong tenant must not stamp last_dedup_checked_at"
    )

    await sc.mark_dedup_checked([mem_id], owner)
    assert (await svc.memory_get_by_id(UUID(mem_id))).last_dedup_checked_at is not None


async def test_mark_dedup_checked_requires_tenant(sc):
    mem_id = await _seed_memory(sc, _t())
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await sc._post("/memories/mark-dedup-checked", {"memory_ids": [mem_id]})
    assert exc.value.response.status_code == 422
