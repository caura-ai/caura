"""A deleted agent's id comes back only at trust 0, until an admin approves it.

``DELETE /agents/{id}`` removed the agent's row and nothing else. The next
heartbeat roster, broker write or read naming the id registered it again at
the default trust, so an admin who deleted an agent to contain it was undone
within one heartbeat.

Full path: core-api over REST -> the in-process storage app (conftest ASGI
bridge) -> test DB.
"""

from __future__ import annotations

import pytest

from core_api.services.agent_service import get_or_create_agent
from tests.conftest import get_test_auth, uid


async def _heartbeat(
    client, headers: dict, tenant_id: str, agent_id: str, fleet_id: str
) -> None:
    response = await client.post(
        "/api/v1/fleet/heartbeat",
        json={
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "node_name": f"node-{agent_id}",
            "agents": [{"agentId": agent_id}],
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text


async def _trust(client, headers: dict, tenant_id: str, agent_id: str) -> int:
    response = await client.get(
        f"/api/v1/agents/{agent_id}?tenant_id={tenant_id}", headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()["trust_level"]


async def _delete(client, headers: dict, tenant_id: str, agent_id: str) -> None:
    response = await client.delete(
        f"/api/v1/agents/{agent_id}?tenant_id={tenant_id}", headers=headers
    )
    assert response.status_code == 204, response.text


async def test_the_next_heartbeat_brings_a_deleted_agent_back_at_trust_0(client):
    tenant_id, headers = get_test_auth()
    agent_id, fleet_id = f"agent-{uid()}", f"fleet-{uid()}"
    await _heartbeat(client, headers, tenant_id, agent_id, fleet_id)
    assert await _trust(client, headers, tenant_id, agent_id) == 1

    await _delete(client, headers, tenant_id, agent_id)
    await _heartbeat(client, headers, tenant_id, agent_id, fleet_id)
    assert await _trust(client, headers, tenant_id, agent_id) == 0


# ``None`` reads the tenant's approval setting, as the heartbeat and read-path
# identity resolution do; the REST bulk write passes ``False``.
@pytest.mark.parametrize("require_approval", [None, False])
async def test_no_first_touch_brings_it_back_trusted(client, require_approval):
    tenant_id, headers = get_test_auth()
    agent_id = f"agent-{uid()}"
    await get_or_create_agent(tenant_id, agent_id, require_approval=require_approval)
    assert await _trust(client, headers, tenant_id, agent_id) == 1

    await _delete(client, headers, tenant_id, agent_id)
    agent = await get_or_create_agent(
        tenant_id, agent_id, require_approval=require_approval
    )
    assert agent["trust_level"] == 0
    assert await _trust(client, headers, tenant_id, agent_id) == 0


async def test_an_admin_raising_its_trust_approves_it(client):
    tenant_id, headers = get_test_auth()
    agent_id, fleet_id = f"agent-{uid()}", f"fleet-{uid()}"
    await _heartbeat(client, headers, tenant_id, agent_id, fleet_id)
    await _delete(client, headers, tenant_id, agent_id)
    await _heartbeat(client, headers, tenant_id, agent_id, fleet_id)

    response = await client.patch(
        f"/api/v1/agents/{agent_id}/trust?tenant_id={tenant_id}",
        json={"trust_level": 1},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    await _heartbeat(client, headers, tenant_id, agent_id, fleet_id)
    assert await _trust(client, headers, tenant_id, agent_id) == 1
