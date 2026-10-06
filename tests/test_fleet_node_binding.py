"""Fleet nodes answer only to the credential they are bound to (M-85).

The heartbeat, the command result and the command listing keyed a node on
caller-named values alone, so an agent or install credential could heartbeat as
another node of its tenant, drain its queue (deploy payloads included) and
report results for its commands. Each node is now bound to the credential that
heartbeats it.

A narrow credential is one the gateway verified: an agent key
(``agent_id_verified``) or an install credential, built here the way auth
Path 4 builds them. Tenant credentials still act as any node of their tenant,
and an ``X-Agent-ID`` a caller asserted itself binds nothing.
"""

from __future__ import annotations

import uuid

import pytest

from core_api import errors
from core_api.app import app
from core_api.auth import AuthContext, get_auth_context
from core_api.tenant_context import set_current_tenant
from tests.conftest import uid

pytestmark = pytest.mark.asyncio


@pytest.fixture
def as_auth():
    """Override get_auth_context with a controlled AuthContext."""

    def _install(tenant_id: str, agent_id: str | None = None, **kwargs):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id,
                agent_id=agent_id,
                readable_tenant_ids=[tenant_id],
                **kwargs,
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


def _agent_key(agent_id: str) -> dict:
    """An agent key whose identity the gateway injected."""
    return {"agent_id": agent_id, "agent_id_verified": True}


def _install_key(install_uuid: str) -> dict:
    return {"is_install_credential": True, "install_uuid": install_uuid}


async def _heartbeat(client, tenant: str, node_name: str):
    return await client.post(
        "/api/v1/fleet/heartbeat", json={"tenant_id": tenant, "node_name": node_name}
    )


async def _queue(client, tenant: str, node_id: str) -> str:
    resp = await client.post(
        "/api/v1/fleet/commands",
        json={"tenant_id": tenant, "node_id": node_id, "command": "ping"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _release(client, tenant: str, node_id: str, **bind: str):
    return await client.post(
        f"/api/v1/fleet/nodes/{node_id}/release", params={"tenant_id": tenant, **bind}
    )


async def _command(client, tenant: str, command_id: str) -> dict:
    listed = await client.get(f"/api/v1/fleet/commands?tenant_id={tenant}")
    assert listed.status_code == 200, listed.text
    return next(c for c in listed.json() if c["id"] == command_id)


async def test_an_agent_cannot_heartbeat_as_another_agents_node(
    client, as_auth, tenant_id
):
    node = f"node-{uid()}"
    as_auth(tenant_id, **_agent_key("owner"))
    assert (await _heartbeat(client, tenant_id, node)).status_code == 200

    as_auth(tenant_id, **_agent_key("intruder"))
    resp = await _heartbeat(client, tenant_id, node)

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == errors.AUTH_FLEET_NODE_BOUND


async def test_an_agent_cannot_drain_a_tenant_nodes_queue(client, as_auth, tenant_id):
    """THE ATTACK: the heartbeat hands over the node's queue and acks it.

    Acked commands are not redelivered, so the intruder would both receive the
    payloads and leave nothing for the real node. Asserts the queue is intact.
    """
    node = f"node-{uid()}"
    as_auth(tenant_id)
    node_id = (await _heartbeat(client, tenant_id, node)).json()["node_id"]
    command_id = await _queue(client, tenant_id, node_id)

    as_auth(tenant_id, **_agent_key("intruder"))
    resp = await _heartbeat(client, tenant_id, node)

    assert resp.status_code == 403, resp.text
    assert command_id not in resp.text
    as_auth(tenant_id)
    assert (await _command(client, tenant_id, command_id))["status"] == "pending"


async def test_an_agent_keeps_its_own_node_and_its_commands(client, as_auth, tenant_id):
    """OVER-REFUSAL GUARD: a node installed with an agent key stays commandable."""
    node = f"node-{uid()}"
    as_auth(tenant_id, **_agent_key("owner"))
    node_id = (await _heartbeat(client, tenant_id, node)).json()["node_id"]
    as_auth(tenant_id)
    command_id = await _queue(client, tenant_id, node_id)

    as_auth(tenant_id, **_agent_key("owner"))
    resp = await _heartbeat(client, tenant_id, node)
    assert resp.status_code == 200, resp.text
    assert [c["id"] for c in resp.json()["commands"]] == [command_id]
    result = await client.post(
        f"/api/v1/fleet/commands/{command_id}/result", json={"status": "done"}
    )
    assert result.status_code == 200, result.text


async def test_a_tenant_credential_takes_a_node_back(client, as_auth, tenant_id):
    # A narrow credential that heartbeats an unbound node before its owner does
    # holds it only until the tenant_id's own next heartbeat.
    node = f"node-{uid()}"
    as_auth(tenant_id, **_agent_key("squatter"))
    assert (await _heartbeat(client, tenant_id, node)).status_code == 200

    as_auth(tenant_id)
    assert (await _heartbeat(client, tenant_id, node)).status_code == 200

    as_auth(tenant_id, **_agent_key("squatter"))
    assert (await _heartbeat(client, tenant_id, node)).status_code == 403


async def test_an_asserted_agent_header_binds_nothing(client, as_auth, tenant_id):
    """OVER-REFUSAL GUARD: shared-key and standalone callers name any agent."""
    node = f"node-{uid()}"
    as_auth(tenant_id, agent_id="one")
    assert (await _heartbeat(client, tenant_id, node)).status_code == 200

    as_auth(tenant_id, agent_id="another")
    assert (await _heartbeat(client, tenant_id, node)).status_code == 200


async def test_an_install_cannot_act_as_another_installs_node(
    client, as_auth, tenant_id
):
    node = f"node-{uid()}"
    as_auth(tenant_id, **_install_key(f"install-{uid()}"))
    assert (await _heartbeat(client, tenant_id, node)).status_code == 200

    as_auth(tenant_id, **_install_key(f"install-{uid()}"))
    assert (await _heartbeat(client, tenant_id, node)).status_code == 403


async def test_an_install_credential_without_a_uuid_is_refused(
    client, as_auth, tenant_id
):
    """Not bound to a shared ``install:unknown``, which every such credential
    would hold, so each could act as the others' nodes."""
    node = f"node-{uid()}"
    as_auth(tenant_id, is_install_credential=True, install_uuid=None)
    resp = await _heartbeat(client, tenant_id, node)

    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == errors.AUTH_INSTALL_UUID_MISSING


async def test_an_agent_cannot_report_another_nodes_command(client, as_auth, tenant_id):
    node = f"node-{uid()}"
    as_auth(tenant_id)
    node_id = (await _heartbeat(client, tenant_id, node)).json()["node_id"]
    command_id = await _queue(client, tenant_id, node_id)

    as_auth(tenant_id, **_agent_key("intruder"))
    resp = await client.post(
        f"/api/v1/fleet/commands/{command_id}/result", json={"status": "done"}
    )

    assert resp.status_code == 404, resp.text
    as_auth(tenant_id)
    assert (await _command(client, tenant_id, command_id))["status"] == "pending"


async def test_an_agent_lists_only_its_own_nodes_commands(client, as_auth, tenant_id):
    as_auth(tenant_id, **_agent_key("owner"))
    own = await _heartbeat(client, tenant_id, f"node-{uid()}")
    as_auth(tenant_id)
    other = await _heartbeat(client, tenant_id, f"node-{uid()}")
    own_command = await _queue(client, tenant_id, own.json()["node_id"])
    other_command = await _queue(client, tenant_id, other.json()["node_id"])

    as_auth(tenant_id, **_agent_key("owner"))
    narrowed = await client.get(f"/api/v1/fleet/commands?tenant_id={tenant_id}")
    as_auth(tenant_id)
    everything = await client.get(f"/api/v1/fleet/commands?tenant_id={tenant_id}")

    assert {c["id"] for c in narrowed.json()} == {own_command}
    assert {c["id"] for c in everything.json()} == {own_command, other_command}


async def test_release_lets_a_rotated_credential_bind_the_node(
    client, as_auth, tenant_id
):
    node = f"node-{uid()}"
    as_auth(tenant_id, **_agent_key("old"))
    node_id = (await _heartbeat(client, tenant_id, node)).json()["node_id"]
    as_auth(tenant_id, **_agent_key("new"))
    assert (await _heartbeat(client, tenant_id, node)).status_code == 403

    as_auth(tenant_id)
    assert (await _release(client, tenant_id, node_id)).status_code == 200

    as_auth(tenant_id, **_agent_key("new"))
    assert (await _heartbeat(client, tenant_id, node)).status_code == 200
    as_auth(tenant_id, **_agent_key("old"))
    assert (await _heartbeat(client, tenant_id, node)).status_code == 403


@pytest.mark.parametrize(
    "new_key,bind",
    [
        pytest.param(_agent_key("new"), {"bind_agent_id": "new"}, id="agent-key"),
        pytest.param(
            _install_key("install-new"),
            {"bind_install_uuid": "install-new"},
            id="install-credential",
        ),
    ],
)
async def test_release_can_bind_the_node_to_a_named_credential(
    client, as_auth, tenant_id, new_key, bind
):
    """A bare release leaves the node for whichever credential heartbeats first.
    Naming the new credential binds it at once, so a key rotation leaves no
    heartbeat in which another narrow credential could claim the node."""
    node = f"node-{uid()}"
    as_auth(tenant_id, **_agent_key("old"))
    node_id = (await _heartbeat(client, tenant_id, node)).json()["node_id"]

    as_auth(tenant_id)
    assert (await _release(client, tenant_id, node_id, **bind)).status_code == 200

    as_auth(tenant_id, **_agent_key("squatter"))
    assert (await _heartbeat(client, tenant_id, node)).status_code == 403
    as_auth(tenant_id, **new_key)
    assert (await _heartbeat(client, tenant_id, node)).status_code == 200


async def test_release_binds_at_most_one_credential(client, as_auth, tenant_id):
    as_auth(tenant_id)
    node_id = (await _heartbeat(client, tenant_id, f"node-{uid()}")).json()["node_id"]

    resp = await _release(
        client, tenant_id, node_id, bind_agent_id="a", bind_install_uuid="b"
    )

    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize(
    "cred",
    [
        pytest.param(_agent_key("owner"), id="agent-key"),
        pytest.param(_install_key("install-1"), id="install-credential"),
    ],
)
async def test_only_a_tenant_credential_can_release_a_node(
    client, as_auth, cred, tenant_id
):
    node = f"node-{uid()}"
    as_auth(tenant_id, **cred)
    node_id = (await _heartbeat(client, tenant_id, node)).json()["node_id"]

    resp = await _release(client, tenant_id, node_id)

    assert resp.status_code == 403, resp.text


async def test_releasing_an_unknown_node_is_404(client, as_auth, tenant_id):
    as_auth(tenant_id)
    resp = await _release(client, tenant_id, str(uuid.uuid4()))

    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "Node not found"
