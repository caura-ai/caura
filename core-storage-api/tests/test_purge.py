"""Integration tests for the org/tenant hard-purge endpoint (CAURA-689).

Uses a fresh, unique tenant_id per test (not the shared session fixture)
so the purge can never wipe data other integration tests seeded under the
shared tenant.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX, _memory_payload

pytestmark = pytest.mark.asyncio


def _fresh_ids() -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    return f"purge-tenant-{suffix}", f"purge-fleet-{suffix}"


class TestPurgeTenantData:
    async def test_purge_deletes_data_and_reports_counts(self, client: AsyncClient) -> None:
        tenant_id, fleet_id = _fresh_ids()
        ids = []
        for _ in range(3):
            resp = await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant_id, fleet_id))
            assert resp.status_code == 200, resp.text
            ids.append(resp.json()["id"])

        resp = await client.post(f"{PREFIX}/purge/tenant-data", json={"tenant_id": tenant_id})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == tenant_id
        deleted = body["deleted"]
        assert deleted["memories"] == 3
        # Every configured table is reported, even when it deleted nothing —
        # the orchestrator records the full per-table breakdown.
        for table in (
            "relations",
            "agents",
            "fleet_nodes",
            "audit_log",
            "documents",
            "organization_settings",
        ):
            assert table in deleted, deleted

        # The memories are physically gone (not just soft-deleted).
        for memory_id in ids:
            got = await client.get(f"{PREFIX}/memories/{memory_id}")
            assert got.status_code == 404

    async def test_purge_is_idempotent(self, client: AsyncClient) -> None:
        tenant_id, fleet_id = _fresh_ids()
        await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant_id, fleet_id))
        first = (await client.post(f"{PREFIX}/purge/tenant-data", json={"tenant_id": tenant_id})).json()
        assert first["deleted"]["memories"] >= 1

        second = (await client.post(f"{PREFIX}/purge/tenant-data", json={"tenant_id": tenant_id})).json()
        assert second["deleted"]["memories"] == 0

    async def test_purge_does_not_touch_other_tenants(self, client: AsyncClient) -> None:
        keep_tenant, keep_fleet = _fresh_ids()
        drop_tenant, drop_fleet = _fresh_ids()
        keep = (await client.post(f"{PREFIX}/memories", json=_memory_payload(keep_tenant, keep_fleet))).json()
        await client.post(f"{PREFIX}/memories", json=_memory_payload(drop_tenant, drop_fleet))

        await client.post(f"{PREFIX}/purge/tenant-data", json={"tenant_id": drop_tenant})

        # The other tenant's memory survives.
        got = await client.get(f"{PREFIX}/memories/{keep['id']}")
        assert got.status_code == 200

    async def test_purge_requires_tenant_id(self, client: AsyncClient) -> None:
        missing = await client.post(f"{PREFIX}/purge/tenant-data", json={})
        assert missing.status_code == 422
        empty = await client.post(f"{PREFIX}/purge/tenant-data", json={"tenant_id": ""})
        assert empty.status_code == 422

    async def test_purge_rejects_malformed_body(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"{PREFIX}/purge/tenant-data",
            content="not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422


class TestPurgeFleetData:
    """Fleet-scoped hard purge: removes one fleet's footprint from a tenant
    while leaving the tenant's other fleets intact."""

    async def _seed_node(self, client: AsyncClient, tenant_id: str, fleet_id: str) -> str:
        resp = await client.post(
            f"{PREFIX}/fleet/nodes",
            json={"tenant_id": tenant_id, "fleet_id": fleet_id, "node_name": f"node-{uuid.uuid4().hex[:8]}"},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["id"]

    async def test_purge_fleet_deletes_scoped_data_and_reports_counts(self, client: AsyncClient) -> None:
        tenant_id, fleet_id = _fresh_ids()
        _, other_fleet = _fresh_ids()  # different fleet, SAME tenant — must survive

        drop_ids = []
        for _ in range(3):
            resp = await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant_id, fleet_id))
            assert resp.status_code == 200, resp.text
            drop_ids.append(resp.json()["id"])
        keep = (await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant_id, other_fleet))).json()

        node_id = await self._seed_node(client, tenant_id, fleet_id)
        cmd = await client.post(
            f"{PREFIX}/fleet/commands",
            json={"tenant_id": tenant_id, "node_id": node_id, "command": "ping"},
        )
        assert cmd.status_code in (200, 201), cmd.text

        resp = await client.post(
            f"{PREFIX}/purge/fleet-data", json={"tenant_id": tenant_id, "fleet_id": fleet_id}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == tenant_id
        assert body["fleet_id"] == fleet_id
        deleted = body["deleted"]
        assert deleted["memories"] == 3
        assert deleted["fleet_nodes"] == 1
        assert deleted["fleet_commands"] == 1
        # Every configured fleet table is reported, even when it deleted nothing.
        for table in (
            "relations",
            "memories",
            "entities",
            "agents",
            "fleet_nodes",
            "fleet_commands",
            "documents",
            "analysis_reports",
            "dedup_reviews",
        ):
            assert table in deleted, deleted

        # The purged fleet's memories are physically gone (not just soft-deleted).
        for memory_id in drop_ids:
            got = await client.get(f"{PREFIX}/memories/{memory_id}")
            assert got.status_code == 404
        # The same tenant's OTHER fleet is untouched.
        survivor = await client.get(f"{PREFIX}/memories/{keep['id']}")
        assert survivor.status_code == 200

    async def test_purge_fleet_is_idempotent(self, client: AsyncClient) -> None:
        tenant_id, fleet_id = _fresh_ids()
        await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant_id, fleet_id))
        first = (
            await client.post(
                f"{PREFIX}/purge/fleet-data", json={"tenant_id": tenant_id, "fleet_id": fleet_id}
            )
        ).json()
        assert first["deleted"]["memories"] >= 1

        second = (
            await client.post(
                f"{PREFIX}/purge/fleet-data", json={"tenant_id": tenant_id, "fleet_id": fleet_id}
            )
        ).json()
        assert second["deleted"]["memories"] == 0
        assert second["deleted"]["fleet_commands"] == 0

    async def test_purge_fleet_does_not_touch_other_tenant(self, client: AsyncClient) -> None:
        keep_tenant, keep_fleet = _fresh_ids()
        drop_tenant, drop_fleet = _fresh_ids()
        keep = (await client.post(f"{PREFIX}/memories", json=_memory_payload(keep_tenant, keep_fleet))).json()
        await client.post(f"{PREFIX}/memories", json=_memory_payload(drop_tenant, drop_fleet))

        await client.post(
            f"{PREFIX}/purge/fleet-data", json={"tenant_id": drop_tenant, "fleet_id": drop_fleet}
        )

        got = await client.get(f"{PREFIX}/memories/{keep['id']}")
        assert got.status_code == 200

    async def test_purge_fleet_requires_tenant_and_fleet(self, client: AsyncClient) -> None:
        tenant_id, fleet_id = _fresh_ids()
        assert (await client.post(f"{PREFIX}/purge/fleet-data", json={})).status_code == 422
        assert (
            await client.post(f"{PREFIX}/purge/fleet-data", json={"tenant_id": tenant_id})
        ).status_code == 422
        assert (
            await client.post(f"{PREFIX}/purge/fleet-data", json={"fleet_id": fleet_id})
        ).status_code == 422
        assert (
            await client.post(f"{PREFIX}/purge/fleet-data", json={"tenant_id": tenant_id, "fleet_id": ""})
        ).status_code == 422

    async def test_purge_fleet_rejects_malformed_body(self, client: AsyncClient) -> None:
        resp = await client.post(
            f"{PREFIX}/purge/fleet-data",
            content="not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 422
