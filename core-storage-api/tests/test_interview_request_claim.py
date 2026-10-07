"""A fleet node's interview window spends the request that invited it (M-86).

``POST /fleet/commands/{id}/claim`` admits one submission per delivered
``interview_request``: the command must be this tenant's, queued for this node,
acked by the node's heartbeat, and neither claimed nor completed yet. The claim
marks the command's ``result``; the node's own result report still overwrites it
and closes the command.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = pytest.mark.asyncio


def _tenant() -> str:
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


async def _node(client: AsyncClient, tenant: str) -> str:
    # A hostname as well: the upsert builds its conflict arm from the non-key
    # columns it is sent, and SQLAlchemy refuses an empty SET even for a new row.
    name = f"node-{uuid.uuid4().hex[:6]}"
    resp = await client.post(
        f"{PREFIX}/fleet/nodes", json={"tenant_id": tenant, "node_name": name, "hostname": "h1"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _command(
    client: AsyncClient, tenant: str, node_id: str, command: str = "interview_request", *, ack: bool = True
) -> str:
    resp = await client.post(
        f"{PREFIX}/fleet/commands",
        json={"tenant_id": tenant, "node_id": node_id, "command": command, "payload": {}},
    )
    assert resp.status_code == 200, resp.text
    command_id = resp.json()["id"]
    if ack:
        acked = await client.post(
            f"{PREFIX}/fleet/commands/ack", json={"tenant_id": tenant, "command_ids": [command_id]}
        )
        assert acked.json()["count"] == 1, acked.text
    return command_id


async def _claim(client: AsyncClient, tenant: str, command_id: str, node_id: str) -> bool:
    resp = await client.post(
        f"{PREFIX}/fleet/commands/{command_id}/claim", json={"tenant_id": tenant, "node_id": node_id}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["ok"]


async def test_a_delivered_request_is_claimed_once(client: AsyncClient) -> None:
    tenant = _tenant()
    node_id = await _node(client, tenant)
    command_id = await _command(client, tenant, node_id)

    assert await _claim(client, tenant, command_id, node_id) is True
    assert await _claim(client, tenant, command_id, node_id) is False


async def test_an_undelivered_request_cannot_be_claimed(client: AsyncClient) -> None:
    tenant = _tenant()
    node_id = await _node(client, tenant)
    command_id = await _command(client, tenant, node_id, ack=False)

    assert await _claim(client, tenant, command_id, node_id) is False


async def test_a_request_is_claimed_only_for_its_own_node(client: AsyncClient) -> None:
    tenant = _tenant()
    node_id, other_id = await _node(client, tenant), await _node(client, tenant)
    command_id = await _command(client, tenant, node_id)

    assert await _claim(client, tenant, command_id, other_id) is False
    assert await _claim(client, tenant, command_id, node_id) is True


async def test_a_claim_is_tenant_scoped(client: AsyncClient) -> None:
    tenant = _tenant()
    node_id = await _node(client, tenant)
    command_id = await _command(client, tenant, node_id)

    assert await _claim(client, _tenant(), command_id, node_id) is False
    assert await _claim(client, tenant, command_id, node_id) is True


async def test_only_an_interview_request_can_be_claimed(client: AsyncClient) -> None:
    tenant = _tenant()
    node_id = await _node(client, tenant)
    command_id = await _command(client, tenant, node_id, "ping")

    assert await _claim(client, tenant, command_id, node_id) is False


async def test_a_completed_request_cannot_be_claimed(client: AsyncClient) -> None:
    tenant = _tenant()
    node_id = await _node(client, tenant)
    command_id = await _command(client, tenant, node_id)
    done = await client.patch(
        f"{PREFIX}/fleet/commands/{command_id}/status",
        json={"tenant_id": tenant, "status": "done", "result": {"ok": True}},
    )
    assert done.json()["ok"] is True, done.text

    assert await _claim(client, tenant, command_id, node_id) is False


async def test_the_nodes_result_report_still_closes_a_claimed_request(client: AsyncClient) -> None:
    tenant = _tenant()
    node_id = await _node(client, tenant)
    command_id = await _command(client, tenant, node_id)
    assert await _claim(client, tenant, command_id, node_id) is True

    done = await client.patch(
        f"{PREFIX}/fleet/commands/{command_id}/status",
        json={"tenant_id": tenant, "status": "done", "result": {"ok": True, "submitted": True}},
    )

    assert done.json()["ok"] is True, done.text
    listed = await client.get(f"{PREFIX}/fleet/commands", params={"tenant_id": tenant})
    row = next(c for c in listed.json() if c["id"] == command_id)
    assert row["status"] == "done"
    assert row["result"] == {"ok": True, "submitted": True}
