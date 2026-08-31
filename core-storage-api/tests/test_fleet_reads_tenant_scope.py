"""The fleet read paths must be told which tenant they are reading for.

The read form of GHSA-wgvw-28pq-jc36 on the fleet tables. Four service methods
keyed on ``node_id`` or ``command_id`` alone, with no tenant predicate, and two
routes that took a node UUID straight off the query string and never mentioned a
tenant at all.

The two routes are the reachable half, and what they disclose is another
tenant's deployment activity:

* ``GET /fleet/commands/in-flight-deploy`` answers whether a deploy is currently
  in flight for a node — so anyone who can reach the port can watch another
  tenant's rollouts happen.
* ``GET /fleet/commands/deploy-attempt-count`` answers how many deploys have
  been queued for a node at a named ``target_version``, which also makes it an
  oracle for which versions another tenant is moving to: ask for a version and a
  non-zero count confirms it.

``fleet_get_node_by_id`` is the third method. It is **not** reachable across
tenants today — all three of its callers guard, two by deriving ``node_id`` from
the tenant-scoped ``fleet_get_node_id`` and the third (``create_command``) by
comparing ``node.tenant_id`` after the fetch. Scoping it removes the unscoped
primitive rather than a live hole, and turns ``create_command``'s manual
comparison into a predicate, which is the form that cannot be forgotten by the
next caller. That distinction is deliberate here: this file does not claim a
cross-tenant read that does not exist.

The fourth, ``fleet_get_command_by_id``, had no callers anywhere in the
repository and is deleted rather than scoped — the third time that question has
paid on this backlog, after #1091 and #1161.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = [pytest.mark.asyncio]


async def _node(client: AsyncClient, tenant_id: str, node_name: str) -> str:
    resp = await client.post(
        f"{PREFIX}/fleet/nodes",
        json={
            "tenant_id": tenant_id,
            "fleet_id": "f1",
            "node_name": node_name,
            "hostname": "h1",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _deploy(client: AsyncClient, tenant_id: str, node_id: str, version: str = "2.4.0") -> None:
    resp = await client.post(
        f"{PREFIX}/fleet/commands",
        json={
            "tenant_id": tenant_id,
            "node_id": node_id,
            "command": "deploy",
            "payload": {"target_version": version},
        },
    )
    assert resp.status_code == 200, resp.text


def _since() -> str:
    return (datetime.now(UTC) - timedelta(hours=1)).isoformat()


class TestInFlightDeployTenantScope:
    async def test_another_tenants_deploy_is_not_visible(self, client: AsyncClient) -> None:
        """The attack: watching a rollout you do not own."""
        victim = f"fl-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"fl-attacker-{uuid.uuid4().hex[:8]}"
        node_id = await _node(client, victim, f"node-{uuid.uuid4().hex[:6]}")
        await _deploy(client, victim, node_id)

        resp = await client.get(
            f"{PREFIX}/fleet/commands/in-flight-deploy",
            params={"node_id": node_id, "since": _since(), "tenant_id": attacker},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["in_flight"] is False, "another tenant's deploy was disclosed"

    async def test_the_owning_tenant_still_sees_its_deploy(self, client: AsyncClient) -> None:
        """The regression guard: scoping must not blind the legitimate caller.

        Without this, returning False unconditionally would pass the test above
        while breaking the deploy-dedup gate the endpoint exists to serve.
        """
        tenant = f"fl-{uuid.uuid4().hex[:8]}"
        node_id = await _node(client, tenant, f"node-{uuid.uuid4().hex[:6]}")
        await _deploy(client, tenant, node_id)

        resp = await client.get(
            f"{PREFIX}/fleet/commands/in-flight-deploy",
            params={"node_id": node_id, "since": _since(), "tenant_id": tenant},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["in_flight"] is True

    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        """No tenant used to mean "any node"; FastAPI now rejects it as missing."""
        tenant = f"fl-{uuid.uuid4().hex[:8]}"
        node_id = await _node(client, tenant, f"node-{uuid.uuid4().hex[:6]}")
        await _deploy(client, tenant, node_id)

        resp = await client.get(
            f"{PREFIX}/fleet/commands/in-flight-deploy",
            params={"node_id": node_id, "since": _since()},
        )

        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.text


class TestDeployAttemptCountTenantScope:
    async def test_another_tenants_attempts_are_not_counted(self, client: AsyncClient) -> None:
        """Also an oracle for which version another tenant is rolling out."""
        victim = f"fl-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"fl-attacker-{uuid.uuid4().hex[:8]}"
        node_id = await _node(client, victim, f"node-{uuid.uuid4().hex[:6]}")
        await _deploy(client, victim, node_id, version="9.9.9")

        resp = await client.get(
            f"{PREFIX}/fleet/commands/deploy-attempt-count",
            params={
                "node_id": node_id,
                "target_version": "9.9.9",
                "since": _since(),
                "tenant_id": attacker,
            },
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 0, "another tenant's deploy attempts were disclosed"

    async def test_the_owning_tenant_still_counts_its_attempts(self, client: AsyncClient) -> None:
        """The attempt budget (CAURA-000) must still see its own deploys."""
        tenant = f"fl-{uuid.uuid4().hex[:8]}"
        node_id = await _node(client, tenant, f"node-{uuid.uuid4().hex[:6]}")
        await _deploy(client, tenant, node_id, version="9.9.9")
        await _deploy(client, tenant, node_id, version="9.9.9")

        resp = await client.get(
            f"{PREFIX}/fleet/commands/deploy-attempt-count",
            params={
                "node_id": node_id,
                "target_version": "9.9.9",
                "since": _since(),
                "tenant_id": tenant,
            },
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["count"] == 2

    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        tenant = f"fl-{uuid.uuid4().hex[:8]}"
        node_id = await _node(client, tenant, f"node-{uuid.uuid4().hex[:6]}")

        resp = await client.get(
            f"{PREFIX}/fleet/commands/deploy-attempt-count",
            params={"node_id": node_id, "target_version": "9.9.9", "since": _since()},
        )

        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.text


class TestCreateCommandNodeOwnership:
    async def test_a_foreign_node_is_still_refused(self, client: AsyncClient) -> None:
        """Regression guard for the check that became a predicate.

        ``create_command`` used to fetch the node unscoped and compare
        ``node.tenant_id`` afterwards. The fetch is scoped now, so the
        comparison is gone; this pins the behaviour it was providing, which is
        the whole reason that comparison existed — an insert naming another
        tenant's node satisfies the FK and would otherwise be collected by that
        node on its next heartbeat.
        """
        victim = f"fl-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"fl-attacker-{uuid.uuid4().hex[:8]}"
        node_id = await _node(client, victim, f"node-{uuid.uuid4().hex[:6]}")

        resp = await client.post(
            f"{PREFIX}/fleet/commands",
            json={
                "tenant_id": attacker,
                "node_id": node_id,
                "command": "deploy",
                "payload": {"target_version": "1.0.0"},
            },
        )

        assert resp.status_code == 404, resp.text

    async def test_the_owning_tenant_can_still_queue(self, client: AsyncClient) -> None:
        """The scoped fetch must not refuse the legitimate insert."""
        tenant = f"fl-{uuid.uuid4().hex[:8]}"
        node_id = await _node(client, tenant, f"node-{uuid.uuid4().hex[:6]}")

        resp = await client.post(
            f"{PREFIX}/fleet/commands",
            json={
                "tenant_id": tenant,
                "node_id": node_id,
                "command": "deploy",
                "payload": {"target_version": "1.0.0"},
            },
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["node_id"] == node_id
