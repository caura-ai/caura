"""``PATCH /fleet/commands/{id}/status`` must be told which tenant it scopes to.

GHSA-xw4x-jwf5-8m9h was a fleet-command route that validated the tenant the
caller named and never checked it against the row it addressed. The fix landed
on the *create* path. The *status* path kept the same hazard in a quieter form:
``tenant_id`` was optional on the wire and defaulted to ``None`` in the SQL
layer, and ``None`` meant "no WHERE clause" — so the cross-tenant UPDATE was
what a caller got by leaving the field out.

Storage authenticates nothing (GHSA-wgvw-28pq-jc36) and trusts the tenant its
caller names, so "the caller will remember" is not a control. These tests pin
the distinction the endpoint now draws: **absent** is a 422, **null** is the
admin opt-out, and a **wrong** tenant matches no row.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from core_storage_api.services.postgres_service import UNSCOPED, Unscoped
from tests.test_integration import PREFIX

# On the class rather than the module: the sentinel check below is synchronous,
# and a module-level asyncio mark warns on every non-async test it covers.


def test_unscoped_sentinel_reads_as_its_name() -> None:
    """A sentinel whose point is being explicit should say so when logged.

    The default ``object`` repr would put a memory address in the log line
    where the reader needs the word "unscoped".
    """
    assert repr(UNSCOPED) == "UNSCOPED"
    assert isinstance(UNSCOPED, Unscoped)


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
    node_id = node.json()["id"]

    command = await client.post(
        f"{PREFIX}/fleet/commands",
        json={
            "tenant_id": tenant_id,
            "node_id": node_id,
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


class TestCommandStatusTenantScope:
    pytestmark = pytest.mark.asyncio

    async def test_omitted_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        """The bug: no ``tenant_id`` key used to mean "update it unscoped"."""
        tenant = f"fleet-scope-{uuid.uuid4().hex[:8]}"
        command_id = await _node_and_command(client, tenant)

        resp = await client.patch(
            f"{PREFIX}/fleet/commands/{command_id}/status",
            json={"status": "done"},
        )

        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.json()["detail"]
        # The row is untouched — the 422 fires before any statement is built.
        assert await _status_of(client, tenant, command_id) == "queued"

    async def test_omitted_tenant_id_cannot_complete_another_tenants_command(
        self, client: AsyncClient
    ) -> None:
        """The reachable consequence, stated as the attack rather than the guard.

        Two tenants, one command each. Addressing the victim's command by UUID
        with no tenant scope is exactly the request the old default served.
        """
        victim = f"fleet-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"fleet-attacker-{uuid.uuid4().hex[:8]}"
        victim_command = await _node_and_command(client, victim)
        await _node_and_command(client, attacker)

        resp = await client.patch(
            f"{PREFIX}/fleet/commands/{victim_command}/status",
            json={"status": "failed", "result": {"error": "forged"}},
        )

        assert resp.status_code == 422, resp.text
        assert await _status_of(client, victim, victim_command) == "queued"

    async def test_wrong_tenant_matches_no_row(self, client: AsyncClient) -> None:
        """Naming a tenant you do not own reports ``ok: false``, not success."""
        victim = f"fleet-victim-{uuid.uuid4().hex[:8]}"
        attacker = f"fleet-attacker-{uuid.uuid4().hex[:8]}"
        victim_command = await _node_and_command(client, victim)

        resp = await client.patch(
            f"{PREFIX}/fleet/commands/{victim_command}/status",
            json={"status": "done", "tenant_id": attacker},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": False}
        assert await _status_of(client, victim, victim_command) == "queued"

    async def test_own_tenant_updates(self, client: AsyncClient) -> None:
        tenant = f"fleet-scope-{uuid.uuid4().hex[:8]}"
        command_id = await _node_and_command(client, tenant)

        resp = await client.patch(
            f"{PREFIX}/fleet/commands/{command_id}/status",
            json={"status": "done", "tenant_id": tenant},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True}
        assert await _status_of(client, tenant, command_id) == "done"

    async def test_explicit_null_is_the_admin_opt_out(self, client: AsyncClient) -> None:
        """core-api sends ``{"tenant_id": auth.tenant_id}`` — ``null`` for admin
        credentials, which legitimately operate across tenants. That request
        must keep working, or the guard above would be a breaking change
        dressed as a fix."""
        tenant = f"fleet-scope-{uuid.uuid4().hex[:8]}"
        command_id = await _node_and_command(client, tenant)

        resp = await client.patch(
            f"{PREFIX}/fleet/commands/{command_id}/status",
            json={"status": "done", "tenant_id": None},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True}
        assert await _status_of(client, tenant, command_id) == "done"

    async def test_non_string_tenant_id_is_rejected(self, client: AsyncClient) -> None:
        """``{"tenant_id": []}`` is neither a tenant nor the admin opt-out.

        Without the type check it reaches SQLAlchemy as a bind parameter and
        surfaces as a 500 — a server fault for what is a malformed request.
        """
        tenant = f"fleet-scope-{uuid.uuid4().hex[:8]}"
        command_id = await _node_and_command(client, tenant)

        resp = await client.patch(
            f"{PREFIX}/fleet/commands/{command_id}/status",
            json={"status": "done", "tenant_id": []},
        )

        assert resp.status_code == 422, resp.text
        assert await _status_of(client, tenant, command_id) == "queued"
