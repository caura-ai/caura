"""``POST /fleet/commands/ack`` must be told which tenant it scopes to.

The third instance of one shape in this subsystem. GHSA-xw4x-jwf5-8m9h was the
*create* path trusting a caller-named tenant without checking the node it
pointed at; ``PATCH /commands/{id}/status`` was the *complete* path, where a
missing ``tenant_id`` meant no WHERE clause. This is the *ack* path, which took
a bare list of command UUIDs and acked every row that matched, whichever tenant
owned it.

Storage authenticates nothing (GHSA-wgvw-28pq-jc36) and trusts the tenant its
caller names, so "the caller will remember" is not a control.

Unlike the status route there is no ``null`` admin opt-out here: nothing acks
across tenants. The only caller is core-api's heartbeat, whose ``tenant_id`` is
a required ``str``.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = [pytest.mark.asyncio]


async def _node_and_command(client: AsyncClient, tenant_id: str) -> str:
    """Create a node + a queued command for ``tenant_id``; return the command id."""
    node = await client.post(
        f"{PREFIX}/fleet/nodes",
        json={
            "tenant_id": tenant_id,
            "fleet_id": "fleet-a",
            "node_name": f"node-{uuid.uuid4().hex[:8]}",
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
    return command.json()["id"]


async def _status_of(client: AsyncClient, tenant_id: str, command_id: str) -> str:
    listed = await client.get(f"{PREFIX}/fleet/commands", params={"tenant_id": tenant_id})
    assert listed.status_code == 200, listed.text
    rows = [r for r in listed.json() if r["id"] == command_id]
    assert rows, f"command {command_id} not visible to {tenant_id}"
    return rows[0]["status"]


class TestFleetAckTenantScope:
    async def test_another_tenants_command_is_not_acked(self, client: AsyncClient) -> None:
        """The attack, stated as the request an attacker would send.

        Knowing a command UUID was enough to ack it. The victim's command must
        be left exactly as it was.
        """
        victim = f"ack-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"ack-attacker-{uuid.uuid4().hex[:8]}"
        victim_command = await _node_and_command(client, victim)
        before = await _status_of(client, victim, victim_command)

        resp = await client.post(
            f"{PREFIX}/fleet/commands/ack",
            json={"command_ids": [victim_command], "tenant_id": attacker},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 0, "the attacker's ack matched a row"
        assert await _status_of(client, victim, victim_command) == before

    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        """No tenant used to mean "ack whatever matches"; it is now a 422."""
        tenant = f"ack-{uuid.uuid4().hex[:8]}"
        command_id = await _node_and_command(client, tenant)

        resp = await client.post(
            f"{PREFIX}/fleet/commands/ack",
            json={"command_ids": [command_id]},
        )

        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.text
        assert await _status_of(client, tenant, command_id) == "queued"

    async def test_a_null_tenant_id_is_rejected_too(self, client: AsyncClient) -> None:
        """No admin opt-out on this route, unlike ``PATCH /commands/{id}/status``.

        That route accepts ``null`` because core-api genuinely completes
        commands as an admin. Nothing acks across tenants, so an opt-out here
        would be an unlocked door with no caller behind it.
        """
        tenant = f"ack-{uuid.uuid4().hex[:8]}"
        command_id = await _node_and_command(client, tenant)

        resp = await client.post(
            f"{PREFIX}/fleet/commands/ack",
            json={"command_ids": [command_id], "tenant_id": None},
        )

        assert resp.status_code == 422, resp.text
        assert await _status_of(client, tenant, command_id) == "queued"

    async def test_own_command_is_acked(self, client: AsyncClient) -> None:
        """The supported call still works, and reports what it did."""
        tenant = f"ack-{uuid.uuid4().hex[:8]}"
        command_id = await _node_and_command(client, tenant)

        resp = await client.post(
            f"{PREFIX}/fleet/commands/ack",
            json={"command_ids": [command_id], "tenant_id": tenant},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "count": 1}
        assert await _status_of(client, tenant, command_id) == "acked"

    async def test_a_mixed_batch_acks_only_the_callers_own(self, client: AsyncClient) -> None:
        """A batch naming both tenants' commands must move only the caller's.

        The scope has to be part of the statement rather than a check on the
        request: a partially-foreign batch is the case a route-level "do these
        all belong to you" guard would reject wholesale, and the SQL predicate
        handles correctly.
        """
        caller = f"ack-caller-{uuid.uuid4().hex[:8]}"
        other = f"ack-other-{uuid.uuid4().hex[:8]}"
        mine = await _node_and_command(client, caller)
        theirs = await _node_and_command(client, other)

        resp = await client.post(
            f"{PREFIX}/fleet/commands/ack",
            json={"command_ids": [mine, theirs], "tenant_id": caller},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 1
        assert await _status_of(client, caller, mine) == "acked"
        assert await _status_of(client, other, theirs) == "queued"

    async def test_a_foreign_id_is_indistinguishable_from_a_missing_one(self, client: AsyncClient) -> None:
        """Both report 0, so the count is not an existence oracle for command UUIDs."""
        victim = f"ack-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"ack-attacker-{uuid.uuid4().hex[:8]}"
        victim_command = await _node_and_command(client, victim)

        foreign = await client.post(
            f"{PREFIX}/fleet/commands/ack",
            json={"command_ids": [victim_command], "tenant_id": attacker},
        )
        missing = await client.post(
            f"{PREFIX}/fleet/commands/ack",
            json={"command_ids": [str(uuid.uuid4())], "tenant_id": attacker},
        )

        assert foreign.status_code == missing.status_code == 200
        assert foreign.json() == missing.json()
