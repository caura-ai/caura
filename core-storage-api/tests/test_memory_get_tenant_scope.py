"""``GET /memories/{memory_id}`` must be told which tenant it is reading for.

The single-row form of GHSA-wgvw-28pq-jc36. ``tenant_id`` was a query parameter
defaulting to ``None``, and ``None`` meant "fetch by primary key with no tenant
predicate" — so a caller who knew a UUID got the row's whole content, whichever
tenant owned it, from a service that authenticates nothing.

``POST /memories/bulk-get`` was the batch form of the same hole, and its own
docstring justified being optional by "mirroring the single-row contract". This
is that contract.
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


class TestMemoryGetTenantScope:
    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        """No tenant used to mean "any tenant"; it is now a 422 from FastAPI."""
        tenant = f"get-{uuid.uuid4().hex[:8]}"
        mem_id = await _memory(client, tenant, "a memory")

        resp = await client.get(f"{PREFIX}/memories/{mem_id}")

        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.text

    async def test_another_tenants_memory_is_not_readable(self, client: AsyncClient) -> None:
        """The attack, stated as the request an attacker would send."""
        victim = f"get-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"get-attacker-{uuid.uuid4().hex[:8]}"
        victim_id = await _memory(client, victim, "victim's secret")

        resp = await client.get(f"{PREFIX}/memories/{victim_id}", params={"tenant_id": attacker})

        assert resp.status_code == 404, resp.text
        assert "victim's secret" not in resp.text

    async def test_a_foreign_id_is_indistinguishable_from_a_missing_one(self, client: AsyncClient) -> None:
        """Both 404, so the endpoint is not an existence oracle for memory UUIDs."""
        victim = f"get-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"get-attacker-{uuid.uuid4().hex[:8]}"
        victim_id = await _memory(client, victim, "victim's secret")

        foreign = await client.get(f"{PREFIX}/memories/{victim_id}", params={"tenant_id": attacker})
        missing = await client.get(f"{PREFIX}/memories/{uuid.uuid4()}", params={"tenant_id": attacker})

        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()

    async def test_own_memory_is_returned(self, client: AsyncClient) -> None:
        tenant = f"get-{uuid.uuid4().hex[:8]}"
        mem_id = await _memory(client, tenant, "mine")

        resp = await client.get(f"{PREFIX}/memories/{mem_id}", params={"tenant_id": tenant})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == mem_id
        assert body["content"] == "mine"

    async def test_a_soft_deleted_memory_is_not_returned(self, client: AsyncClient) -> None:
        """Held by the same query now that the predicate moved into SQL.

        The scoped read used to filter ``deleted_at`` in Python after the fetch;
        this pins that the behaviour survived moving it into the statement.
        """
        tenant = f"get-{uuid.uuid4().hex[:8]}"
        mem_id = await _memory(client, tenant, "doomed")

        deleted = await client.delete(f"{PREFIX}/memories/{mem_id}", params={"tenant_id": tenant})
        assert deleted.status_code == 200, deleted.text

        resp = await client.get(f"{PREFIX}/memories/{mem_id}", params={"tenant_id": tenant})
        assert resp.status_code == 404, resp.text
