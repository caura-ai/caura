"""A deleted agent leaves a tombstone, and storage creates its id at trust 0.

``DELETE /agents/{id}`` removed the agent's row and nothing else, so any later
create of the id, from core-api's registration or from agent-key provisioning,
got whatever trust its caller asked for.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import select

from common.models import AgentTombstone
from core_storage_api.services.postgres_service import get_session
from tests.test_integration import PREFIX


async def _delete(client: AsyncClient, tenant_id: str, agent_id: str) -> None:
    response = await client.delete(f"{PREFIX}/agents/{agent_id}", params={"tenant_id": tenant_id})
    assert response.status_code == 200, response.text


async def _tombstoned(tenant_id: str, agent_id: str) -> bool:
    async with get_session() as session:
        found = await session.execute(
            select(AgentTombstone.agent_id).where(
                AgentTombstone.tenant_id == tenant_id, AgentTombstone.agent_id == agent_id
            )
        )
        return found.first() is not None


async def _trust(client: AsyncClient, tenant_id: str, agent_id: str) -> int:
    response = await client.get(f"{PREFIX}/agents/{agent_id}", params={"tenant_id": tenant_id})
    assert response.status_code == 200, response.text
    return response.json()["trust_level"]


async def test_both_create_routes_bring_a_deleted_agent_back_at_trust_0(
    client: AsyncClient, tenant_id: str, fleet_id: str
) -> None:
    for route in ("/agents", "/agents/create-only"):
        agent_id = f"agent-tombstone-{uuid.uuid4().hex[:8]}"
        body = {"tenant_id": tenant_id, "agent_id": agent_id, "fleet_id": fleet_id, "trust_level": 2}
        created = await client.post(f"{PREFIX}{route}", json=body)
        assert created.status_code == 200, created.text
        assert created.json()["trust_level"] == 2

        await _delete(client, tenant_id, agent_id)
        again = await client.post(f"{PREFIX}{route}", json=body)
        assert again.status_code == 200, again.text
        assert again.json()["trust_level"] == 0, route
        assert await _trust(client, tenant_id, agent_id) == 0, route


async def test_only_an_admin_changes_a_tombstoned_agents_trust(client: AsyncClient, tenant_id: str) -> None:
    """The upsert overwrites an existing row's trust when its caller sends one,
    so a registration that lost the race to create the row would raise it. The
    admin's approval removes the tombstone: the id is an ordinary agent again."""
    agent_id = f"agent-tombstone-{uuid.uuid4().hex[:8]}"
    body = {"tenant_id": tenant_id, "agent_id": agent_id, "trust_level": 1}
    assert (await client.post(f"{PREFIX}/agents", json=body)).status_code == 200
    await _delete(client, tenant_id, agent_id)
    assert await _tombstoned(tenant_id, agent_id)

    for _ in range(2):
        response = await client.post(f"{PREFIX}/agents", json=body)
        assert response.status_code == 200, response.text
        assert await _trust(client, tenant_id, agent_id) == 0

    approved = await client.patch(
        f"{PREFIX}/agents/{agent_id}/trust-level", json={"tenant_id": tenant_id, "trust_level": 2}
    )
    assert approved.status_code == 200, approved.text
    assert await _trust(client, tenant_id, agent_id) == 2
    assert not await _tombstoned(tenant_id, agent_id)


async def test_an_agent_never_deleted_is_created_as_asked(client: AsyncClient, tenant_id: str) -> None:
    agent_id = f"agent-tombstone-{uuid.uuid4().hex[:8]}"
    missing = await client.delete(f"{PREFIX}/agents/{agent_id}", params={"tenant_id": tenant_id})
    assert missing.status_code == 404, missing.text
    body = {"tenant_id": tenant_id, "agent_id": agent_id, "trust_level": 2}
    created = await client.post(f"{PREFIX}/agents", json=body)
    assert created.status_code == 200, created.text
    assert created.json()["trust_level"] == 2
