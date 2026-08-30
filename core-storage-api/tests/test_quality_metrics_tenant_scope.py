"""``POST /memories/quality-metrics`` requires a binding tenant.

The guard was ``if not tenant_id and not readable_tenant_ids``, so a request
carrying only a ``readable_tenant_ids`` list passed it. That grant is read
verbatim from the request body by a service that authenticates nothing
(GHSA-wgvw-28pq-jc36), so accepting it in place of a home tenant let the caller
name its own scope.

This was the only one of the eight ``readable_tenant_ids`` routes shaped that
way — the other seven require ``tenant_id`` and treat the grant as a widening
on top of it, so omitting the grant narrows to the home tenant rather than
opening up. The route's own error message already read ``tenant_id is
required``.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = [pytest.mark.asyncio]


async def _memory(client: AsyncClient, tenant_id: str, content: str) -> str:
    resp = await client.post(
        f"{PREFIX}/memories",
        json={
            "tenant_id": tenant_id,
            "agent_id": "agent-a",
            "content": content,
            "memory_type": "fact",
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


class TestQualityMetricsTenantScope:
    async def test_a_grant_alone_is_rejected(self, client: AsyncClient) -> None:
        """The defect, stated as the request an attacker would send.

        No ``tenant_id``, only a list of tenants the caller nominated. This used
        to reach the query.
        """
        victim = f"qm-victim-{uuid.uuid4().hex[:8]}"
        await _memory(client, victim, "victim's secret")

        resp = await client.post(
            f"{PREFIX}/memories/quality-metrics",
            json={"readable_tenant_ids": [victim]},
        )

        assert resp.status_code == 422, resp.text
        assert "tenant_id" in resp.text

    async def test_no_scope_at_all_is_rejected(self, client: AsyncClient) -> None:
        resp = await client.post(f"{PREFIX}/memories/quality-metrics", json={})
        assert resp.status_code == 422, resp.text

    async def test_own_tenant_is_returned(self, client: AsyncClient) -> None:
        """The supported call still works."""
        tenant = f"qm-{uuid.uuid4().hex[:8]}"
        await _memory(client, tenant, "mine")

        resp = await client.post(
            f"{PREFIX}/memories/quality-metrics",
            json={"tenant_id": tenant},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] >= 1

    async def test_the_grant_still_widens_on_top_of_a_tenant(self, client: AsyncClient) -> None:
        """The grant is not removed — it is only stopped from standing alone.

        With a binding tenant present it still widens the read across the named
        siblings, which is what the other seven routes do and what this change
        must not break.
        """
        home = f"qm-home-{uuid.uuid4().hex[:8]}"
        sibling = f"qm-sib-{uuid.uuid4().hex[:8]}"
        await _memory(client, home, "home memory")
        await _memory(client, sibling, "sibling memory")

        narrow = await client.post(
            f"{PREFIX}/memories/quality-metrics",
            json={"tenant_id": home},
        )
        widened = await client.post(
            f"{PREFIX}/memories/quality-metrics",
            json={"tenant_id": home, "readable_tenant_ids": [home, sibling]},
        )

        assert narrow.status_code == widened.status_code == 200, widened.text
        assert widened.json()["total"] > narrow.json()["total"]

    async def test_omitting_the_grant_narrows_to_the_home_tenant(self, client: AsyncClient) -> None:
        """Fail-closed: forgetting the grant must not widen the read.

        This is the property that makes the grant safe to omit, and it only
        holds because a binding tenant is now always present.
        """
        home = f"qm-home-{uuid.uuid4().hex[:8]}"
        other = f"qm-other-{uuid.uuid4().hex[:8]}"
        await _memory(client, home, "home memory")
        await _memory(client, other, "other tenant's memory")

        resp = await client.post(
            f"{PREFIX}/memories/quality-metrics",
            json={"tenant_id": home},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 1
