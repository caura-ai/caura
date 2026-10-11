"""An unknown node name is not a server error on the by-name fleet endpoints.

``fleet_get_node_id`` used ``scalar_one()``, which raises ``NoResultFound`` on a
name this tenant does not have, so every by-name fleet endpoint answered a typo
with a 500.

This file also pinned ``DELETE /fleet/nodes/{node_name}`` and its ``GET`` twin.
Neither had a caller, since core-api exposes no single-node read or delete, and
both were deleted (L-216).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = [pytest.mark.asyncio]


async def _node(client: AsyncClient, tenant_id: str, node_name: str, fleet_id: str | None = "f1") -> str:
    resp = await client.post(
        f"{PREFIX}/fleet/nodes",
        json={
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "node_name": node_name,
            "hostname": "h1",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _command(client: AsyncClient, tenant_id: str, node_id: str) -> None:
    resp = await client.post(
        f"{PREFIX}/fleet/commands",
        json={
            "tenant_id": tenant_id,
            "node_id": node_id,
            "command": "deploy",
            "payload": {"target_version": "2.4.0"},
        },
    )
    assert resp.status_code == 200, resp.text


class TestUnknownNodeNameIsNotAServerError:
    """Both by-name endpoints, because one ``scalar_one()`` fed them."""

    async def test_pending_commands(self, client: AsyncClient) -> None:
        tenant = f"fd-{uuid.uuid4().hex[:8]}"
        resp = await client.get(
            f"{PREFIX}/fleet/commands/pending", params={"tenant_id": tenant, "node_name": "nope"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    async def test_commands_by_unknown_name_do_not_widen_to_the_whole_tenant(
        self, client: AsyncClient
    ) -> None:
        """The trap in fixing the 500 the obvious way.

        ``fleet_list_commands(node_id=None)`` drops the node filter, so falling
        through with the unresolved None would answer a typo with every command
        in the tenant — a worse failure than the 500 it replaced.
        """
        tenant = f"fd-{uuid.uuid4().hex[:8]}"
        name = f"node-{uuid.uuid4().hex[:6]}"
        node_id = await _node(client, tenant, name)
        await _command(client, tenant, node_id)

        resp = await client.get(f"{PREFIX}/fleet/commands", params={"tenant_id": tenant, "node_name": "nope"})
        assert resp.status_code == 200, resp.text
        assert resp.json() == [], "an unknown node name listed the tenant's other commands"
