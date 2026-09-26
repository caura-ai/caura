"""``GET /fleet/nodes/summary``: recently-seen node count and distinct plugin versions.

Backs ``counts.plugin_nodes_7d`` / ``counts.plugin_versions`` in the anonymous
heartbeat (core-api ``heartbeat/payload.py``). Tenant-scoped like every other
fleet read; core-api fans it out over the active tenants.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = [pytest.mark.asyncio]


async def _node(
    client: AsyncClient,
    tenant_id: str,
    node_name: str,
    *,
    plugin_version: str | None,
    seen_ago: timedelta,
) -> None:
    body = {
        "tenant_id": tenant_id,
        "fleet_id": "f1",
        "node_name": node_name,
        "hostname": "h1",
        "plugin_version": plugin_version,
        "last_heartbeat": (datetime.now(UTC) - seen_ago).isoformat(),
    }
    resp = await client.post(f"{PREFIX}/fleet/nodes", json=body)
    assert resp.status_code == 200, resp.text


async def test_summary_counts_recent_nodes_and_distinct_versions(client: AsyncClient) -> None:
    tenant_a = f"hb-a-{uuid.uuid4().hex[:8]}"
    tenant_b = f"hb-b-{uuid.uuid4().hex[:8]}"
    await _node(client, tenant_a, "n1", plugin_version="2.21.3", seen_ago=timedelta(minutes=1))
    await _node(client, tenant_a, "n2", plugin_version="2.21.0", seen_ago=timedelta(hours=1))
    await _node(client, tenant_a, "n2b", plugin_version="2.21.0", seen_ago=timedelta(days=2))
    await _node(client, tenant_a, "n3", plugin_version="2.19.0", seen_ago=timedelta(days=10))
    await _node(client, tenant_a, "n4", plugin_version=None, seen_ago=timedelta(minutes=5))
    await _node(client, tenant_b, "n5", plugin_version="9.9.9", seen_ago=timedelta(minutes=1))

    resp = await client.get(f"{PREFIX}/fleet/nodes/summary", params={"tenant_id": tenant_a})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"nodes_7d": 4, "plugin_versions": ["2.21.0", "2.21.3"]}

    resp = await client.get(f"{PREFIX}/fleet/nodes/summary", params={"tenant_id": tenant_b})
    assert resp.json() == {"nodes_7d": 1, "plugin_versions": ["9.9.9"]}


async def test_summary_window_is_configurable(client: AsyncClient) -> None:
    tenant = f"hb-w-{uuid.uuid4().hex[:8]}"
    await _node(client, tenant, "old", plugin_version="2.19.0", seen_ago=timedelta(days=10))
    await _node(client, tenant, "new", plugin_version="2.21.0", seen_ago=timedelta(hours=2))

    resp = await client.get(f"{PREFIX}/fleet/nodes/summary", params={"tenant_id": tenant, "days": 30})
    assert resp.json() == {"nodes_7d": 2, "plugin_versions": ["2.19.0", "2.21.0"]}
    resp = await client.get(f"{PREFIX}/fleet/nodes/summary", params={"tenant_id": tenant, "days": 1})
    assert resp.json() == {"nodes_7d": 1, "plugin_versions": ["2.21.0"]}


async def test_summary_empty_tenant(client: AsyncClient) -> None:
    resp = await client.get(
        f"{PREFIX}/fleet/nodes/summary", params={"tenant_id": f"hb-none-{uuid.uuid4().hex[:8]}"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"nodes_7d": 0, "plugin_versions": []}


async def test_summary_requires_a_tenant(client: AsyncClient) -> None:
    resp = await client.get(f"{PREFIX}/fleet/nodes/summary")
    assert resp.status_code == 422


@pytest.mark.parametrize("days", [0, -1, 366])
async def test_summary_rejects_out_of_range_windows(client: AsyncClient, days: int) -> None:
    resp = await client.get(f"{PREFIX}/fleet/nodes/summary", params={"tenant_id": "t", "days": days})
    assert resp.status_code == 422


async def test_summary_does_not_shadow_get_node(client: AsyncClient) -> None:
    """``/nodes/summary`` is a literal segment; ``/nodes/{node_name}`` still resolves."""
    tenant = f"hb-s-{uuid.uuid4().hex[:8]}"
    await _node(client, tenant, "summary-lookalike", plugin_version="1.0.0", seen_ago=timedelta(0))
    resp = await client.get(f"{PREFIX}/fleet/nodes/summary-lookalike", params={"tenant_id": tenant})
    assert resp.status_code == 200
    assert resp.json()["node_name"] == "summary-lookalike"
