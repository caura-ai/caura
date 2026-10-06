"""Credential provisioning cannot turn a registration race into an upsert."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import AsyncClient

from core_storage_api.services.postgres_service import PostgresService
from tests.test_integration import PREFIX


async def test_create_only_rejects_missing_or_mismatched_tenant(client: AsyncClient, tenant_id: str) -> None:
    response = await client.post(f"{PREFIX}/agents/create-only", json={"agent_id": "main"})
    assert response.status_code == 422

    with pytest.raises(ValueError, match="tenant_id does not match"):
        await PostgresService().agent_create_only(
            tenant_id, {"tenant_id": "other-tenant", "agent_id": "main"}
        )


async def test_create_only_route_preserves_existing_trust_fleet_and_name(
    client: AsyncClient, tenant_id: str, fleet_id: str
) -> None:
    agent_id = f"agent-create-only-{uuid.uuid4().hex[:8]}"
    original = {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "fleet_id": fleet_id,
        "trust_level": 3,
        "display_name": "owner-chosen",
    }
    created = await client.post(f"{PREFIX}/agents/create-only", json=original)
    assert created.status_code == 200, created.text
    assert created.json()["created"] is True

    attempted_overwrite = {
        **original,
        "fleet_id": "attacker-fleet",
        "trust_level": 0,
        "display_name": "replacement",
    }
    response = await client.post(f"{PREFIX}/agents/create-only", json=attempted_overwrite)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == created.json()["id"]
    assert response.json()["created"] is False

    stored = await client.get(f"{PREFIX}/agents/{agent_id}", params={"tenant_id": tenant_id})
    assert stored.status_code == 200, stored.text
    assert stored.json()["fleet_id"] == fleet_id
    assert stored.json()["trust_level"] == 3
    assert stored.json()["display_name"] == "owner-chosen"


async def test_concurrent_create_only_keeps_one_complete_initial_identity(
    tenant_id: str, fleet_id: str
) -> None:
    agent_id = f"agent-create-race-{uuid.uuid4().hex[:8]}"
    svc = PostgresService()
    first = {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "fleet_id": fleet_id,
        "trust_level": 1,
        "display_name": "first",
    }
    second = {
        **first,
        "fleet_id": "other-fleet",
        "trust_level": 3,
        "display_name": "second",
    }

    (left, left_created), (right, right_created) = await asyncio.gather(
        svc.agent_create_only(tenant_id, first),
        svc.agent_create_only(tenant_id, second),
    )
    stored = await svc.agent_get_by_id(agent_id, tenant_id)
    assert stored is not None
    assert left.id == right.id == stored.id
    assert {left_created, right_created} == {True, False}
    assert (stored.fleet_id, stored.trust_level, stored.display_name) in {
        (first["fleet_id"], first["trust_level"], first["display_name"]),
        (second["fleet_id"], second["trust_level"], second["display_name"]),
    }
