"""``POST /memories/bulk-get`` must be told which tenant it is reading for.

This is GHSA-wgvw-28pq-jc36's exploit primitive. The route accepted a list of
ids and an **optional** ``tenant_id``; omit it and every id you named came back,
whichever tenant owned it, from a service that authenticates nothing. The
docstring justified the optionality as mirroring the single-row
``GET /memories/{id}`` contract — which only meant two endpoints shared a hole.

The filter is now required *and* applied in SQL. Both halves matter and are
tested separately: requiring it closes the omission, and pushing it into the
query means a foreign row is never selected rather than selected and then
dropped by a Python comparison that a later refactor could quietly remove.
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


class TestBulkGetTenantScope:
    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        """The advisory: no tenant key used to mean "any tenant"."""
        tenant = f"bulk-{uuid.uuid4().hex[:8]}"
        mem_id = await _memory(client, tenant, "a memory")

        resp = await client.post(f"{PREFIX}/memories/bulk-get", json={"ids": [mem_id]})

        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.json()["detail"]

    async def test_another_tenants_id_reads_as_absent(self, client: AsyncClient) -> None:
        """Naming a foreign id returns ``None`` in its slot, not the row.

        Not a 404 for the whole request: a foreign id has to be
        indistinguishable from a nonexistent one, or the endpoint becomes an
        existence oracle for memory UUIDs.
        """
        victim = f"bulk-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"bulk-attacker-{uuid.uuid4().hex[:8]}"
        victim_id = await _memory(client, victim, "victim's secret")
        attacker_id = await _memory(client, attacker, "attacker's own")

        resp = await client.post(
            f"{PREFIX}/memories/bulk-get",
            json={"ids": [attacker_id, victim_id], "tenant_id": attacker},
        )

        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert len(rows) == 2
        assert rows[0]["id"] == attacker_id
        assert rows[1] is None, "another tenant's memory came back"

    async def test_nonexistent_and_foreign_ids_are_indistinguishable(self, client: AsyncClient) -> None:
        victim = f"bulk-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"bulk-attacker-{uuid.uuid4().hex[:8]}"
        victim_id = await _memory(client, victim, "victim's secret")

        resp = await client.post(
            f"{PREFIX}/memories/bulk-get",
            json={"ids": [victim_id, str(uuid.uuid4())], "tenant_id": attacker},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json() == [None, None]

    async def test_own_ids_are_returned_in_input_order(self, client: AsyncClient) -> None:
        """Order preservation is load-bearing — callers zip the response back."""
        tenant = f"bulk-{uuid.uuid4().hex[:8]}"
        first = await _memory(client, tenant, "first")
        second = await _memory(client, tenant, "second")
        missing = str(uuid.uuid4())

        resp = await client.post(
            f"{PREFIX}/memories/bulk-get",
            json={"ids": [second, missing, first], "tenant_id": tenant},
        )

        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert [r["id"] if r else None for r in rows] == [second, None, first]

    async def test_empty_id_list_still_requires_the_tenant(self, client: AsyncClient) -> None:
        """The guard runs before the empty-list short-circuit.

        Otherwise ``{"ids": []}`` would be the one body that skips validation,
        and the contract would depend on how many ids you happened to send.
        """
        resp = await client.post(f"{PREFIX}/memories/bulk-get", json={"ids": []})
        assert resp.status_code == 422, resp.text
