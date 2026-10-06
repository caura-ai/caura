"""The conflict list can be narrowed to the records that name one memory (M-102, M-34).

Dismissing a conflict reverts its loser only when no other standing verdict
still demotes it. A verdict that left no chain edge is known only from its
``memory_conflicts`` record, so the undo asks for every record that names the
loser, on either side. Both columns are indexed (``ix_memory_conflicts_new``,
``ix_memory_conflicts_old``). The tenant stays the boundary.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest
from httpx import AsyncClient

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PREFIX = "/api/v1/storage"


async def _memory(client: AsyncClient, tenant_id: str, fleet_id: str) -> str:
    content = f"conflict filter {uuid.uuid4().hex}"
    resp = await client.post(
        f"{PREFIX}/memories",
        json={
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "agent_id": "conflict-filter-tester",
            "memory_type": "fact",
            "content": content,
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            "visibility": "scope_team",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _record(client: AsyncClient, tenant_id: str, fleet_id: str, new_id: str, old_id: str) -> str:
    resp = await client.post(
        f"{PREFIX}/memories/conflicts",
        json={
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "new_memory_id": new_id,
            "old_memory_id": old_id,
            "relationship": "exact_value",
            "action": "supersede",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _listed(client: AsyncClient, tenant_id: str, **params: str) -> set[str]:
    resp = await client.get(f"{PREFIX}/memories/memory-conflicts", params={"tenant_id": tenant_id, **params})
    assert resp.status_code == 200, resp.text
    return {row["id"] for row in resp.json()}


async def test_the_list_narrows_to_the_records_naming_a_memory_on_either_side(
    client: AsyncClient, tenant_id: str, fleet_id: str
) -> None:
    a, b, c = [await _memory(client, tenant_id, fleet_id) for _ in range(3)]
    a_won = await _record(client, tenant_id, fleet_id, a, b)
    a_lost = await _record(client, tenant_id, fleet_id, c, a)
    unrelated = await _record(client, tenant_id, fleet_id, b, c)

    assert await _listed(client, tenant_id, memory_id=a) == {a_won, a_lost}
    # The fixture's tenant is shared, so the unfiltered queue may be long; narrow by b instead.
    assert await _listed(client, tenant_id, memory_id=b) == {a_won, unrelated}


async def test_the_narrowed_list_stays_inside_the_tenant(
    client: AsyncClient, tenant_id: str, fleet_id: str
) -> None:
    a, b = [await _memory(client, tenant_id, fleet_id) for _ in range(2)]
    await _record(client, tenant_id, fleet_id, a, b)

    assert await _listed(client, f"other-{tenant_id}", memory_id=a) == set()


async def test_a_memory_id_that_is_not_a_uuid_is_a_400(client: AsyncClient, tenant_id: str) -> None:
    resp = await client.get(
        f"{PREFIX}/memories/memory-conflicts", params={"tenant_id": tenant_id, "memory_id": "not-a-uuid"}
    )
    assert resp.status_code == 400, resp.text
