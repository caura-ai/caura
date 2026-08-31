"""Tenant scoping for search-profile updates addressed by agent primary key."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from core_storage_api.services.postgres_service import PostgresService
from tests.test_integration import PREFIX

pytestmark = [pytest.mark.asyncio]


def _new_tenant_id() -> str:
    """Return a unique tenant id that matches the end-of-run sweep prefix."""
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


async def _agent(client: AsyncClient, tenant_id: str, profile: dict) -> tuple[str, str]:
    logical_id = f"profile-agent-{uuid.uuid4().hex[:8]}"
    response = await client.post(
        f"{PREFIX}/agents",
        json={
            "tenant_id": tenant_id,
            "agent_id": logical_id,
            "search_profile": profile,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["id"], logical_id


async def _profile(client: AsyncClient, tenant_id: str, agent_id: str) -> dict | None:
    response = await client.get(
        f"{PREFIX}/agents/{agent_id}/search-profile",
        params={"tenant_id": tenant_id},
    )
    assert response.status_code == 200, response.text
    return response.json()["search_profile"]


class TestAgentUpdateSearchProfileTenantScope:
    async def test_service_does_not_update_another_tenants_profile(
        self,
        client: AsyncClient,
    ) -> None:
        victim = _new_tenant_id()
        attacker = _new_tenant_id()
        original = {"min_similarity": 0.61}
        agent_pk, agent_id = await _agent(client, victim, original)

        await PostgresService().agent_update_search_profile(
            agent_pk,
            tenant_id=attacker,
            search_profile={"min_similarity": 0.22},
        )

        assert await _profile(client, victim, agent_id) == original

    async def test_route_passes_tenant_to_profile_update(self, client: AsyncClient) -> None:
        tenant = _new_tenant_id()
        agent_pk, agent_id = await _agent(client, tenant, {"min_similarity": 0.58})
        updated = {"min_similarity": 0.73}

        response = await client.patch(
            f"{PREFIX}/agents/{agent_pk}/search-profile",
            json={"tenant_id": tenant, "search_profile": updated},
        )

        assert response.status_code == 200, response.text
        assert await _profile(client, tenant, agent_id) == updated
