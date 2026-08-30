"""``DELETE /memories/{memory_id}`` must be told which tenant it deletes for.

The write form of GHSA-wgvw-28pq-jc36. The route took a bare UUID and
soft-deleted by primary key with no tenant predicate, so a caller who knew an
id could destroy another tenant's memory through a service that authenticates
nothing. ``GET /memories/{memory_id}`` was the read form and was fixed in
caura-ai/caura#1075; this is the same row, addressed the same way, where the
consequence is destruction rather than disclosure.

The fix routes through ``memory_soft_delete_by_ids``, which was already
tenant-scoped and already backs the bulk delete routes, rather than scoping a
second single-row path to the same write. That method also filters
``deleted_at IS NULL``, which is why a repeat delete now 404s instead of
re-stamping ``deleted_at`` and moving the retention clock.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = [pytest.mark.asyncio]


async def _memory(client: AsyncClient, tenant_id: str, content: str) -> str:
    resp = await client.post(
        f"{PREFIX}/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": "agent-a",
            "content": content,
            "memory_type": "fact",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _is_live(client: AsyncClient, tenant_id: str, memory_id: str) -> bool:
    resp = await client.get(f"{PREFIX}/memories/{memory_id}", params={"tenant_id": tenant_id})
    return resp.status_code == 200


class TestMemoryDeleteTenantScope:
    async def test_another_tenants_memory_is_not_deleted(self, client: AsyncClient) -> None:
        """The attack, stated as the request an attacker would send."""
        victim = f"del-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"del-attacker-{uuid.uuid4().hex[:8]}"
        victim_id = await _memory(client, victim, "victim's memory")

        resp = await client.delete(f"{PREFIX}/memories/{victim_id}", params={"tenant_id": attacker})

        assert resp.status_code == 404, resp.text
        assert await _is_live(client, victim, victim_id), "the victim's memory was deleted"

    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        """No tenant used to mean "delete by primary key"; it is now a 422."""
        tenant = f"del-{uuid.uuid4().hex[:8]}"
        memory_id = await _memory(client, tenant, "mine")

        resp = await client.delete(f"{PREFIX}/memories/{memory_id}")

        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.text
        assert await _is_live(client, tenant, memory_id)

    async def test_own_memory_is_deleted(self, client: AsyncClient) -> None:
        """The supported call still works."""
        tenant = f"del-{uuid.uuid4().hex[:8]}"
        memory_id = await _memory(client, tenant, "doomed")

        resp = await client.delete(f"{PREFIX}/memories/{memory_id}", params={"tenant_id": tenant})

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True}
        assert not await _is_live(client, tenant, memory_id)

    async def test_a_foreign_id_is_indistinguishable_from_a_missing_one(self, client: AsyncClient) -> None:
        """Both 404, so the endpoint is not an existence oracle for memory UUIDs."""
        victim = f"del-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"del-attacker-{uuid.uuid4().hex[:8]}"
        victim_id = await _memory(client, victim, "victim's memory")

        foreign = await client.delete(f"{PREFIX}/memories/{victim_id}", params={"tenant_id": attacker})
        missing = await client.delete(f"{PREFIX}/memories/{uuid.uuid4()}", params={"tenant_id": attacker})

        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()

    async def test_deleting_twice_does_not_move_the_retention_clock(self, client: AsyncClient) -> None:
        """A repeat delete is a 404, not a silent re-stamp of ``deleted_at``.

        The old single-row path had no ``deleted_at IS NULL`` guard, so a second
        DELETE rewrote the timestamp and pushed the purge window out. Routing
        through ``memory_soft_delete_by_ids`` fixes that as a side effect of
        scoping it, and this pins the behaviour so a future single-row variant
        cannot quietly reintroduce it.
        """
        tenant = f"del-{uuid.uuid4().hex[:8]}"
        memory_id = await _memory(client, tenant, "doomed")

        first = await client.delete(f"{PREFIX}/memories/{memory_id}", params={"tenant_id": tenant})
        second = await client.delete(f"{PREFIX}/memories/{memory_id}", params={"tenant_id": tenant})

        assert first.status_code == 200, first.text
        assert second.status_code == 404, second.text
