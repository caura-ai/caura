"""``agent_reset_search_profile`` must bind its primary-key update to a tenant.

The HTTP route already looked the agent up within the caller's tenant before
resetting it.  This is defence-in-depth for the service method itself: the
query must remain safe when a future caller does not repeat that lookup.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from core_storage_api.services.postgres_service import PostgresService
from tests.test_integration import PREFIX

pytestmark = [pytest.mark.asyncio]


async def _agent(client: AsyncClient, tenant_id: str, profile: dict) -> tuple[str, str]:
    logical_id = f"agent-{uuid.uuid4().hex[:8]}"
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


class TestAgentResetSearchProfileTenantScope:
    async def test_service_does_not_reset_another_tenants_profile(self, client: AsyncClient) -> None:
        victim = f"reset-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"reset-attacker-{uuid.uuid4().hex[:8]}"
        profile = {"min_similarity": 0.61}
        agent_pk, agent_id = await _agent(client, victim, profile)

        await PostgresService().agent_reset_search_profile(agent_pk, tenant_id=attacker)

        assert await _profile(client, victim, agent_id) == profile

    async def test_service_resets_own_tenants_profile(self, client: AsyncClient) -> None:
        tenant = f"reset-{uuid.uuid4().hex[:8]}"
        agent_pk, agent_id = await _agent(client, tenant, {"min_similarity": 0.58})

        await PostgresService().agent_reset_search_profile(agent_pk, tenant_id=tenant)

        assert await _profile(client, tenant, agent_id) is None
