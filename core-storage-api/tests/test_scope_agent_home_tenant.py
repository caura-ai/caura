"""A caller's own ``scope_agent`` rows are its rows in its HOME tenant.

``agent_id`` is unique per tenant only (``uq_agents_tenant_agent``), so two
tenants can each run an agent called ``rollup-bot`` and they are different
agents. Every scoped read used to admit ``visibility = 'scope_agent' AND
agent_id = :caller`` with no tenant attached, so once the tenant predicate
widened to ``readable_tenant_ids`` (a cross-tenant credential) the sibling
tenant's same-named agent's private rows came back as the caller's own.

Covered here: ``/memories/list``, ``/memories/scored-search``,
and ``/memories/load-by-ids`` — with and without
an explicit ``caller_tenant_id`` (it defaults to the request ``tenant_id``) —
plus the no-identity allow-list.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX, _memory_payload, fake_embedding

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

AGENT = "rollup-bot"

_SEARCH_PARAMS = {
    "fts_weight": 0.5,
    "freshness_floor": 0.5,
    "freshness_decay_days": 30.0,
    "recall_boost_cap": 2.0,
    "recall_decay_window_days": 7.0,
    "similarity_blend": 0.7,
}


async def _private(client: AsyncClient, tenant: str, fleet: str, content: str) -> str:
    payload = _memory_payload(tenant, fleet, content=content)
    payload["agent_id"] = AGENT
    payload["visibility"] = "scope_agent"
    resp = await client.post(f"{PREFIX}/memories", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


@pytest.fixture
async def two_tenants(client: AsyncClient) -> tuple[str, str, str, str, str]:
    suffix = uuid.uuid4().hex[:8]
    home, sibling, fleet = f"home-{suffix}", f"sibling-{suffix}", f"fleet-{suffix}"
    keyword = f"rollupnote{suffix}"
    own = await _private(client, home, fleet, f"home private {keyword}")
    foreign = await _private(client, sibling, fleet, f"sibling private {keyword}")
    return home, sibling, keyword, own, foreign


class TestScopeAgentHomeTenant:
    async def test_list_widened_read_returns_only_home_private_rows(
        self, client: AsyncClient, two_tenants
    ) -> None:
        home, sibling, _, own, foreign = two_tenants
        for extra in ({}, {"caller_tenant_id": home}):
            resp = await client.post(
                f"{PREFIX}/memories/list",
                json={
                    "tenant_id": home,
                    "caller_agent_id": AGENT,
                    "readable_tenant_ids": [home, sibling],
                    "limit": 50,
                    **extra,
                },
            )
            assert resp.status_code == 200, resp.text
            ids = {r["id"] for r in resp.json()}
            assert own in ids
            assert foreign not in ids, "a sibling tenant's same-named agent's private row came back"

    async def test_list_pinned_sibling_read_returns_no_private_rows(
        self, client: AsyncClient, two_tenants
    ) -> None:
        """A pinned read of the sibling still carries the HOME tenant as the
        caller's — the request ``tenant_id`` is the sibling there."""
        home, sibling, _, _, foreign = two_tenants
        resp = await client.post(
            f"{PREFIX}/memories/list",
            json={"tenant_id": sibling, "caller_agent_id": AGENT, "caller_tenant_id": home, "limit": 50},
        )
        assert resp.status_code == 200, resp.text
        assert foreign not in {r["id"] for r in resp.json()}

    async def test_scored_search_widened_read_excludes_sibling_private_rows(
        self, client: AsyncClient, two_tenants
    ) -> None:
        home, sibling, keyword, own, foreign = two_tenants
        resp = await client.post(
            f"{PREFIX}/memories/scored-search",
            json={
                "tenant_id": home,
                "embedding": fake_embedding(keyword),
                "query": keyword,
                "caller_agent_id": AGENT,
                "readable_tenant_ids": [home, sibling],
                "top_k": 10,
                "search_params": _SEARCH_PARAMS,
            },
        )
        assert resp.status_code == 200, resp.text
        ids = {r["id"] for r in resp.json()}
        assert own in ids
        assert foreign not in ids

    async def test_load_by_ids_widened_read_excludes_sibling_private_rows(
        self, client: AsyncClient, two_tenants
    ) -> None:
        home, sibling, _, own, foreign = two_tenants
        resp = await client.post(
            f"{PREFIX}/memories/load-by-ids",
            json={
                "tenant_id": home,
                "memory_ids": [own, foreign],
                "caller_agent_id": AGENT,
                "readable_tenant_ids": [home, sibling],
            },
        )
        assert resp.status_code == 200, resp.text
        assert {r["id"] for r in resp.json()} == {own}

    async def test_no_identity_admits_only_known_shared_visibilities(self, client: AsyncClient) -> None:
        """Without an agent identity the predicate is an allow-list: a row
        carrying a visibility outside the known three is not shared."""
        suffix = uuid.uuid4().hex[:8]
        tenant, fleet = f"vis-{suffix}", f"fleet-{suffix}"
        team = await client.post(f"{PREFIX}/memories", json=_memory_payload(tenant, fleet))
        assert team.status_code == 200, team.text
        odd_payload = _memory_payload(tenant, fleet)
        odd_payload["visibility"] = "scope_unknown"
        odd = await client.post(f"{PREFIX}/memories", json=odd_payload)
        assert odd.status_code == 200, odd.text

        resp = await client.post(f"{PREFIX}/memories/list", json={"tenant_id": tenant, "limit": 50})
        assert resp.status_code == 200, resp.text
        ids = {r["id"] for r in resp.json()}
        assert team.json()["id"] in ids
        assert odd.json()["id"] not in ids
