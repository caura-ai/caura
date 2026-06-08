"""E2E agent trust-level management tests through HTTP API — real DB, no mocks."""

import uuid

import pytest

from tests.conftest import get_test_auth, uid as _uid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _write_memory(client, tenant_id: str, headers: dict, content: str,
                        agent_id: str = "test-agent",
                        fleet_id: str | None = None) -> dict:
    """Write a single memory (which auto-creates the agent)."""
    body = {
        "tenant_id": tenant_id,
        "content": content,
        "agent_id": agent_id,
        "memory_type": "fact",
    }
    if fleet_id:
        body["fleet_id"] = fleet_id
    resp = await client.post(
        "/api/v1/memories",
        json=body,
        headers=headers,
    )
    assert resp.status_code == 201, f"Write failed: {resp.text}"
    return resp.json()


async def _get_agents(client, tenant_id: str, headers: dict) -> list:
    resp = await client.get(
        f"/api/v1/agents?tenant_id={tenant_id}",
        headers=headers,
    )
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_agent_created_on_memory_write(client):
    """Writing a memory with agent_id auto-creates the agent with trust_level=1."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent_name = f"agent-{tag}"

    await _write_memory(client, tenant_id, headers,
                        f"Some fact about agents for testing [{tag}]",
                        agent_id=agent_name, fleet_id=f"fleet-{tag}")

    agents = await _get_agents(client, tenant_id, headers)
    matching = [a for a in agents if a["agent_id"] == agent_name]
    assert len(matching) == 1
    assert matching[0]["trust_level"] == 1  # default


async def test_update_agent_trust_level(client):
    """PATCH /api/agents/{agent_id}/trust?tenant_id=X updates the trust level."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent_name = f"agent-{tag}"

    await _write_memory(client, tenant_id, headers,
                        f"A fact about trust levels for testing [{tag}]",
                        agent_id=agent_name, fleet_id=f"fleet-{tag}")

    # Update trust level to 3 via the canonical /trust route.
    resp = await client.patch(
        f"/api/v1/agents/{agent_name}/trust?tenant_id={tenant_id}",
        json={"trust_level": 3},
        headers=headers,
    )
    assert resp.status_code == 200

    agents = await _get_agents(client, tenant_id, headers)
    matching = [a for a in agents if a["agent_id"] == agent_name]
    assert len(matching) == 1
    assert matching[0]["trust_level"] == 3


async def test_multiple_agents_per_tenant(client):
    """Writing memories with 3 different agent_ids creates 3 distinct agents."""
    tenant_id, headers = get_test_auth()
    tag = _uid()

    names = [f"agent-{tag}-{i}" for i in range(3)]
    for name in names:
        await _write_memory(client, tenant_id, headers,
                            f"Memory from {name} [{tag}]",
                            agent_id=name, fleet_id=f"fleet-{tag}")

    agents = await _get_agents(client, tenant_id, headers)
    agent_ids = {a["agent_id"] for a in agents}
    for name in names:
        assert name in agent_ids


async def test_agent_fleet_association(client):
    """Writing memory with agent_id + fleet_id associates the agent with that fleet."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    agent_name = f"agent-{tag}"
    fleet_id = f"fleet-{tag}"

    await _write_memory(client, tenant_id, headers,
                        f"Fleet memory [{tag}]",
                        agent_id=agent_name, fleet_id=fleet_id)

    agents = await _get_agents(client, tenant_id, headers)
    matching = [a for a in agents if a["agent_id"] == agent_name]
    assert len(matching) == 1
    assert matching[0]["fleet_id"] == fleet_id


async def test_agent_isolation(client):
    """Agents created under one tenant are visible (single-tenant standalone mode)."""
    tenant_id, headers = get_test_auth()
    tag = _uid()

    agent_a = f"agent-a-{tag}"
    agent_b = f"agent-b-{tag}"

    await _write_memory(client, tenant_id, headers,
                        f"A's memory [{tag}]",
                        agent_id=agent_a, fleet_id=f"fleet-a-{tag}")
    await _write_memory(client, tenant_id, headers,
                        f"B's memory [{tag}]",
                        agent_id=agent_b, fleet_id=f"fleet-b-{tag}")

    agents = await _get_agents(client, tenant_id, headers)
    agent_ids = {a["agent_id"] for a in agents}
    assert agent_a in agent_ids
    assert agent_b in agent_ids
