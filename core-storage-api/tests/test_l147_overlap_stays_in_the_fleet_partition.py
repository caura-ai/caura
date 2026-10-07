"""Path C compares a memory only with rows of its own fleet partition (L-147, M-124).

The RDF and semantic finders group ``fleet_id`` as ``COALESCE(fleet_id, '')``,
so a memory with no fleet is compared with fleet-less rows only. The
entity-overlap finder behind Path C added its fleet predicate only when a fleet
was given, so a memory with no fleet was compared with the rows of every fleet
that shared an entity name. A confirmed verdict then demoted and linked a row
of a fleet its writer may not read, and lineage showed that row to anyone who
could read the fleet-less memory (M-124).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX, _memory_payload

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _linked_memory(client: AsyncClient, tenant: str, fleet_id: str | None, name: str) -> str:
    """A live memory in ``fleet_id`` linked to an entity called ``name``."""
    entity = await client.post(
        f"{PREFIX}/entities",
        json={"tenant_id": tenant, "entity_type": "person", "canonical_name": name},
    )
    assert entity.status_code == 200, entity.text
    memory = await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant, fleet_id))
    assert memory.status_code == 200, memory.text
    mid = memory.json()["id"]
    link = await client.post(
        f"{PREFIX}/entities/links",
        json={"tenant_id": tenant, "memory_id": mid, "entity_id": entity.json()["id"], "role": "subject"},
    )
    assert link.status_code == 200, link.text
    return mid


async def _candidates(client: AsyncClient, tenant: str, fleet_id: str | None, memory_id: str) -> set[str]:
    resp = await client.post(
        f"{PREFIX}/memories/entity-overlap-candidates",
        json={"tenant_id": tenant, "fleet_id": fleet_id, "memory_id": memory_id},
    )
    assert resp.status_code == 200, resp.text
    return {row["id"] for row in resp.json()}


@pytest.mark.parametrize("fleet_id", [None, ""])
async def test_a_memory_with_no_fleet_meets_only_fleet_less_rows(
    client: AsyncClient, fleet_id: str | None
) -> None:
    """``''`` and NULL are one partition, as for the other two finders (M-60)."""
    tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"
    name = f"Overlap-{uuid.uuid4().hex}"
    memory = await _linked_memory(client, tenant, fleet_id, name)
    no_fleet = await _linked_memory(client, tenant, None, name)
    empty_fleet = await _linked_memory(client, tenant, "", name)
    await _linked_memory(client, tenant, "fleet-x", name)

    assert await _candidates(client, tenant, fleet_id, memory) == {no_fleet, empty_fleet}


async def test_a_memory_in_a_fleet_meets_only_that_fleets_rows(client: AsyncClient) -> None:
    tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"
    name = f"Overlap-{uuid.uuid4().hex}"
    memory = await _linked_memory(client, tenant, "fleet-x", name)
    same_fleet = await _linked_memory(client, tenant, "fleet-x", name)
    await _linked_memory(client, tenant, None, name)
    await _linked_memory(client, tenant, "fleet-y", name)

    assert await _candidates(client, tenant, "fleet-x", memory) == {same_fleet}
