"""``DELETE /fleet/nodes/{node_name}`` deletes the node.

It used to delete the node's COMMANDS and return ``{"ok": True}`` with the row
still in place — so the node kept reporting in, kept appearing in
``GET /fleet/nodes``, and a second DELETE succeeded exactly as loudly as the
first. Nothing about the response distinguished the two.

The lookup underneath is the other half. ``fleet_get_node_id`` used
``scalar_one()``, which raises ``NoResultFound`` on a name this tenant does not
have, so all four by-name fleet endpoints answered a typo with a 500 — and the
``if node is None: 404`` two lines below two of those calls was unreachable,
because nothing ever returned to compare against None.
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


class TestDeleteActuallyDeletes:
    async def test_the_node_is_gone_afterwards(self, client: AsyncClient) -> None:
        """The whole finding, stated as the caller experiences it."""
        tenant = f"fd-{uuid.uuid4().hex[:8]}"
        name = f"node-{uuid.uuid4().hex[:6]}"
        await _node(client, tenant, name)

        resp = await client.delete(f"{PREFIX}/fleet/nodes/{name}", params={"tenant_id": tenant})
        assert resp.status_code == 200, resp.text

        after = await client.get(f"{PREFIX}/fleet/nodes/{name}", params={"tenant_id": tenant})
        assert after.status_code == 404, f"node still present after DELETE: {after.text}"

    async def test_it_disappears_from_the_fleet_listing_too(self, client: AsyncClient) -> None:
        """A single-row read could be satisfied by a filter; the listing could not."""
        tenant = f"fd-{uuid.uuid4().hex[:8]}"
        name = f"node-{uuid.uuid4().hex[:6]}"
        await _node(client, tenant, name)

        listed = await client.get(f"{PREFIX}/fleet/nodes", params={"tenant_id": tenant})
        assert any(n["node_name"] == name for n in listed.json()), "fixture node was not created"

        await client.delete(f"{PREFIX}/fleet/nodes/{name}", params={"tenant_id": tenant})

        listed = await client.get(f"{PREFIX}/fleet/nodes", params={"tenant_id": tenant})
        assert not any(n["node_name"] == name for n in listed.json())

    async def test_a_second_delete_is_a_404(self, client: AsyncClient) -> None:
        """Both calls used to return ``{"ok": True}``, which is how a delete
        that deletes nothing passes for one that works."""
        tenant = f"fd-{uuid.uuid4().hex[:8]}"
        name = f"node-{uuid.uuid4().hex[:6]}"
        await _node(client, tenant, name)

        first = await client.delete(f"{PREFIX}/fleet/nodes/{name}", params={"tenant_id": tenant})
        assert first.status_code == 200, first.text

        second = await client.delete(f"{PREFIX}/fleet/nodes/{name}", params={"tenant_id": tenant})
        assert second.status_code == 404, second.text

    async def test_the_nodes_commands_go_with_it(self, client: AsyncClient) -> None:
        """The command cleanup was the one thing the old handler did do, and it
        did it only when the node had a ``fleet_id`` — a condition a delete does
        not depend on. Deleting an unfleeted node left its commands behind."""
        tenant = f"fd-{uuid.uuid4().hex[:8]}"
        name = f"node-{uuid.uuid4().hex[:6]}"
        node_id = await _node(client, tenant, name, fleet_id=None)
        await _command(client, tenant, node_id)

        pending = await client.get(
            f"{PREFIX}/fleet/commands/pending", params={"tenant_id": tenant, "node_name": name}
        )
        assert pending.json(), "fixture command was not created"

        resp = await client.delete(f"{PREFIX}/fleet/nodes/{name}", params={"tenant_id": tenant})
        assert resp.status_code == 200, resp.text

        listed = await client.get(
            f"{PREFIX}/fleet/commands", params={"tenant_id": tenant, "node_id": node_id}
        )
        assert listed.json() == [], "commands outlived the node they belonged to"

    async def test_another_tenant_cannot_delete_it(self, client: AsyncClient) -> None:
        tenant = f"fd-{uuid.uuid4().hex[:8]}"
        other = f"fd-other-{uuid.uuid4().hex[:8]}"
        name = f"node-{uuid.uuid4().hex[:6]}"
        await _node(client, tenant, name)

        resp = await client.delete(f"{PREFIX}/fleet/nodes/{name}", params={"tenant_id": other})
        assert resp.status_code == 404, resp.text

        still = await client.get(f"{PREFIX}/fleet/nodes/{name}", params={"tenant_id": tenant})
        assert still.status_code == 200, "the owning tenant's node was deleted by another tenant"


class TestUnknownNodeNameIsNotAServerError:
    """All four by-name endpoints, because one ``scalar_one()`` fed them all."""

    async def test_get_node(self, client: AsyncClient) -> None:
        tenant = f"fd-{uuid.uuid4().hex[:8]}"
        resp = await client.get(f"{PREFIX}/fleet/nodes/nope", params={"tenant_id": tenant})
        assert resp.status_code == 404, resp.text

    async def test_delete_node(self, client: AsyncClient) -> None:
        tenant = f"fd-{uuid.uuid4().hex[:8]}"
        resp = await client.delete(f"{PREFIX}/fleet/nodes/nope", params={"tenant_id": tenant})
        assert resp.status_code == 404, resp.text

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
