"""A fleet node answers only to the credential it is bound to (M-85).

``POST /fleet/nodes`` keyed a node on its caller-supplied ``node_name`` alone,
so any credential that could reach the heartbeat could act as any node of its
tenant. ``owner_principal`` binds the node, and ``fleet_upsert_node`` applies
the rule in its conflict arm, against the row the statement locked:

* a new or unbound node takes the credential that heartbeats it;
* a narrow credential (``agent:<id>``, ``install:<uuid>``) is refused on a node
  bound to another, and the refused heartbeat changes nothing;
* a tenant-wide credential is always admitted and takes the node back;
* ``POST /fleet/nodes/{id}/release`` clears the binding for the next heartbeat,
  or binds the node straight to a named credential.

Command results and listings narrow to the caller's bound nodes the same way.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = pytest.mark.asyncio


def _ids() -> tuple[str, str]:
    return f"test-tenant-{uuid.uuid4().hex[:8]}", f"node-{uuid.uuid4().hex[:6]}"


async def _heartbeat(client: AsyncClient, tenant: str, name: str, principal: str | None, **extra: object):
    body = {"tenant_id": tenant, "fleet_id": "f1", "node_name": name, "hostname": "h1", **extra}
    if principal is not None:
        body["owner_principal"] = principal
    return await client.post(f"{PREFIX}/fleet/nodes", json=body)


async def _row(client: AsyncClient, tenant: str, name: str) -> dict:
    resp = await client.get(f"{PREFIX}/fleet/nodes/{name}", params={"tenant_id": tenant})
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _command(client: AsyncClient, tenant: str, node_id: str) -> str:
    resp = await client.post(
        f"{PREFIX}/fleet/commands",
        json={"tenant_id": tenant, "node_id": node_id, "command": "ping", "payload": {}},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def test_a_new_node_binds_to_the_credential_that_registers_it(client: AsyncClient) -> None:
    tenant, name = _ids()
    resp = await _heartbeat(client, tenant, name, "agent:a")
    assert resp.status_code == 200, resp.text
    assert (await _row(client, tenant, name))["owner_principal"] == "agent:a"


async def test_another_narrow_credential_is_refused_and_changes_nothing(client: AsyncClient) -> None:
    tenant, name = _ids()
    assert (await _heartbeat(client, tenant, name, "agent:a")).status_code == 200

    resp = await _heartbeat(client, tenant, name, "agent:b", hostname="elsewhere")

    assert resp.status_code == 409, resp.text
    row = await _row(client, tenant, name)
    assert row["owner_principal"] == "agent:a"
    assert row["hostname"] == "h1"


async def test_the_bound_credential_keeps_refreshing_its_node(client: AsyncClient) -> None:
    tenant, name = _ids()
    first = await _heartbeat(client, tenant, name, "agent:a")

    again = await _heartbeat(client, tenant, name, "agent:a", hostname="h2")

    assert again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]
    assert (await _row(client, tenant, name))["hostname"] == "h2"


async def test_a_tenant_credential_takes_the_node_back(client: AsyncClient) -> None:
    # A narrow credential that heartbeats an unbound node before its real owner
    # does holds it only until the tenant's own next heartbeat.
    tenant, name = _ids()
    assert (await _heartbeat(client, tenant, name, "agent:b")).status_code == 200

    resp = await _heartbeat(client, tenant, name, "tenant")

    assert resp.status_code == 200, resp.text
    assert (await _row(client, tenant, name))["owner_principal"] == "tenant"
    assert (await _heartbeat(client, tenant, name, "agent:b")).status_code == 409


async def test_an_unbound_node_binds_on_its_next_heartbeat(client: AsyncClient) -> None:
    # A row from before binding shipped carries none, as does one written by a
    # caller that names no credential (the ``POST /fleet`` sentinel).
    tenant, name = _ids()
    assert (await _heartbeat(client, tenant, name, None)).status_code == 200
    assert (await _row(client, tenant, name))["owner_principal"] is None

    assert (await _heartbeat(client, tenant, name, "install:i1")).status_code == 200

    assert (await _row(client, tenant, name))["owner_principal"] == "install:i1"


async def test_a_caller_naming_no_credential_leaves_the_binding(client: AsyncClient) -> None:
    tenant, name = _ids()
    assert (await _heartbeat(client, tenant, name, "agent:a")).status_code == 200

    assert (await _heartbeat(client, tenant, name, None, hostname="h2")).status_code == 200

    assert (await _row(client, tenant, name))["owner_principal"] == "agent:a"


async def test_release_lets_the_next_heartbeat_bind_the_node(client: AsyncClient) -> None:
    tenant, name = _ids()
    node_id = (await _heartbeat(client, tenant, name, "agent:a")).json()["id"]

    resp = await client.post(f"{PREFIX}/fleet/nodes/{node_id}/release", json={"tenant_id": tenant})

    assert resp.status_code == 200, resp.text
    assert (await _heartbeat(client, tenant, name, "agent:b")).status_code == 200
    assert (await _row(client, tenant, name))["owner_principal"] == "agent:b"


async def test_release_can_bind_the_node_to_a_named_credential(client: AsyncClient) -> None:
    # No open window: the node answers to the named credential at once.
    tenant, name = _ids()
    node_id = (await _heartbeat(client, tenant, name, "agent:a")).json()["id"]

    resp = await client.post(
        f"{PREFIX}/fleet/nodes/{node_id}/release", json={"tenant_id": tenant, "owner_principal": "agent:b"}
    )

    assert resp.status_code == 200, resp.text
    assert (await _row(client, tenant, name))["owner_principal"] == "agent:b"
    assert (await _heartbeat(client, tenant, name, "agent:c")).status_code == 409
    assert (await _heartbeat(client, tenant, name, "agent:b")).status_code == 200


async def test_release_is_tenant_scoped(client: AsyncClient) -> None:
    tenant, name = _ids()
    node_id = (await _heartbeat(client, tenant, name, "agent:a")).json()["id"]

    resp = await client.post(f"{PREFIX}/fleet/nodes/{node_id}/release", json={"tenant_id": f"other-{tenant}"})

    assert resp.status_code == 404, resp.text
    assert (await _row(client, tenant, name))["owner_principal"] == "agent:a"


async def test_a_command_result_is_bound_to_its_nodes_credential(client: AsyncClient) -> None:
    tenant, name = _ids()
    node_id = (await _heartbeat(client, tenant, name, "agent:a")).json()["id"]
    command_id = await _command(client, tenant, node_id)
    url = f"{PREFIX}/fleet/commands/{command_id}/status"

    refused = await client.patch(
        url, json={"tenant_id": tenant, "status": "done", "owner_principal": "agent:b"}
    )
    accepted = await client.patch(
        url, json={"tenant_id": tenant, "status": "done", "owner_principal": "agent:a"}
    )

    assert refused.json() == {"ok": False}
    assert accepted.json() == {"ok": True}


async def test_a_command_listing_narrows_to_the_credentials_nodes(client: AsyncClient) -> None:
    tenant, own_name = _ids()
    _, other_name = _ids()
    own_node = (await _heartbeat(client, tenant, own_name, "agent:a")).json()["id"]
    other_node = (await _heartbeat(client, tenant, other_name, "tenant")).json()["id"]
    own_command = await _command(client, tenant, own_node)
    other_command = await _command(client, tenant, other_node)

    narrowed = await client.get(
        f"{PREFIX}/fleet/commands", params={"tenant_id": tenant, "owner_principal": "agent:a"}
    )
    everything = await client.get(f"{PREFIX}/fleet/commands", params={"tenant_id": tenant})

    assert {c["id"] for c in narrowed.json()} == {own_command}
    assert {c["id"] for c in everything.json()} == {own_command, other_command}
