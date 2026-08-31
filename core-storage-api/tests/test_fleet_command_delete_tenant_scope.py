"""``fleet_delete_commands_for_nodes`` must bind deletes to a tenant.

The route already resolves a node within the caller's tenant before invoking
the method. This is defence-in-depth for the service method: a future caller
must not be able to delete another tenant's commands with a known node UUID.
"""

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


async def _node_and_command(client: AsyncClient, tenant_id: str) -> tuple[str, str, str]:
    node_name = f"delete-node-{uuid.uuid4().hex[:8]}"
    node = await client.post(
        f"{PREFIX}/fleet/nodes",
        json={
            "tenant_id": tenant_id,
            "fleet_id": "fleet-a",
            "node_name": node_name,
        },
    )
    assert node.status_code == 200, node.text

    command = await client.post(
        f"{PREFIX}/fleet/commands",
        json={
            "tenant_id": tenant_id,
            "node_id": node.json()["id"],
            "command": "deploy",
            "status": "queued",
        },
    )
    assert command.status_code == 200, command.text
    return node.json()["id"], node_name, command.json()["id"]


async def _command_ids(client: AsyncClient, tenant_id: str) -> set[str]:
    response = await client.get(f"{PREFIX}/fleet/commands", params={"tenant_id": tenant_id})
    assert response.status_code == 200, response.text
    return {row["id"] for row in response.json()}


class TestFleetCommandDeleteTenantScope:
    async def test_service_deletes_only_the_callers_commands(self, client: AsyncClient) -> None:
        caller = _new_tenant_id()
        other = _new_tenant_id()
        caller_node, _, caller_command = await _node_and_command(client, caller)
        other_node, _, other_command = await _node_and_command(client, other)

        await PostgresService().fleet_delete_commands_for_nodes(
            tenant_id=caller,
            node_ids=[uuid.UUID(caller_node), uuid.UUID(other_node)],
        )

        assert caller_command not in await _command_ids(client, caller)
        assert other_command in await _command_ids(client, other)

    async def test_route_passes_the_tenant_to_the_delete(self, client: AsyncClient) -> None:
        tenant = _new_tenant_id()
        _, node_name, command_id = await _node_and_command(client, tenant)

        response = await client.delete(
            f"{PREFIX}/fleet/nodes/{node_name}",
            params={"tenant_id": tenant},
        )

        assert response.status_code == 200, response.text
        assert command_id not in await _command_ids(client, tenant)
