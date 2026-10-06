"""Entity overlap must scope the seed as well as its candidates (audit L-76)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from core_storage_api.services.postgres_service import get_session
from tests.test_integration import PREFIX, _memory_payload

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _linked_memory(client: AsyncClient, tenant: str, name: str, status: str) -> str:
    entity = await client.post(
        f"{PREFIX}/entities",
        json={"tenant_id": tenant, "entity_type": "person", "canonical_name": name},
    )
    assert entity.status_code == 200, entity.text
    memory = await client.post(
        f"{PREFIX}/memories",
        json={**_memory_payload(tenant, "fleet-overlap"), "status": status},
    )
    assert memory.status_code == 200, memory.text
    mid = memory.json()["id"]
    link = await client.post(
        f"{PREFIX}/entities/links",
        json={"tenant_id": tenant, "memory_id": mid, "entity_id": entity.json()["id"], "role": "subject"},
    )
    assert link.status_code == 200, link.text
    return mid


@pytest.mark.parametrize("seed_scope", ["own", "foreign", "missing"])
@pytest.mark.parametrize("include_supersedes", [False, True])
async def test_seed_ownership_precedes_entity_overlap(
    client: AsyncClient, seed_scope: str, include_supersedes: bool
) -> None:
    tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"
    other = f"test-tenant-{uuid.uuid4().hex[:8]}"
    name = f"Overlap-{uuid.uuid4().hex}"
    active = await _linked_memory(client, tenant, name, "active")
    conflicted = await _linked_memory(client, tenant, name, "conflicted")
    if seed_scope == "missing":
        seed = str(uuid.uuid4())
    else:
        seed = await _linked_memory(client, tenant if seed_scope == "own" else other, name, "active")
        # Raw SQL models historical cross-tenant pointers that current writes
        # refuse. A foreign seed must not expose either overlap or its chain.
        async with get_session() as session:
            await session.execute(
                text(
                    "UPDATE memories SET supersedes_id = CAST(:target AS uuid) WHERE id = CAST(:seed AS uuid)"
                ),
                {"target": conflicted, "seed": seed},
            )
            await session.commit()

    result = await client.post(
        f"{PREFIX}/memories/entity-overlap-candidates",
        json={
            "tenant_id": tenant,
            "fleet_id": "fleet-overlap",
            "memory_id": seed,
            "include_supersedes": include_supersedes,
        },
    )
    assert result.status_code == 200, result.text
    ids = {row["id"] for row in result.json()}
    if seed_scope == "own":
        assert ids == ({active, conflicted} if include_supersedes else {active})
    else:
        assert ids == set()
