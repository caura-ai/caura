"""``include_scope_agent`` on ``/memories/list``: every scope for a person.

core-api sets it, with no ``caller_agent_id``, for a signed-in person, who sees
every memory scope (the Prism decision record, §3). Pinned here:

- with it, the list returns every agent's ``scope_agent`` rows, and rows whose
  visibility is outside the known three;
- without it, nothing changes: no identity still lists only shared rows;
- it is ignored when ``caller_agent_id`` is set, as on ``/stats-breakdown``,
  so an agent identity can never use it to read a peer's private rows;
- ``/stats-breakdown`` and ``/count-active`` count exactly what the person's
  list returns.

The core-api half is ``tests/test_person_sees_every_scope.py``.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX, _memory_payload

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _write(client: AsyncClient, tenant: str, fleet: str, agent: str, visibility: str) -> str:
    payload = _memory_payload(tenant, fleet)
    payload["agent_id"] = agent
    payload["visibility"] = visibility
    resp = await client.post(f"{PREFIX}/memories", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


@pytest.fixture
async def tenant_rows(client: AsyncClient) -> tuple[str, dict[str, str]]:
    suffix = uuid.uuid4().hex[:8]
    tenant, fleet = f"person-{suffix}", f"fleet-{suffix}"
    rows = {
        "team": await _write(client, tenant, fleet, "alpha", "scope_team"),
        "alpha_private": await _write(client, tenant, fleet, "alpha", "scope_agent"),
        "beta_private": await _write(client, tenant, fleet, "beta", "scope_agent"),
        "odd": await _write(client, tenant, fleet, "beta", "scope_unknown"),
    }
    return tenant, rows


async def _list(client: AsyncClient, tenant: str, **extra) -> set[str]:
    resp = await client.post(f"{PREFIX}/memories/list", json={"tenant_id": tenant, "limit": 50, **extra})
    assert resp.status_code == 200, resp.text
    return {r["id"] for r in resp.json()}


class TestListIncludeScopeAgent:
    async def test_without_an_identity_it_lists_every_row(self, client: AsyncClient, tenant_rows) -> None:
        tenant, rows = tenant_rows
        assert await _list(client, tenant, include_scope_agent=True) == set(rows.values())

    async def test_unset_keeps_the_shared_only_default(self, client: AsyncClient, tenant_rows) -> None:
        tenant, rows = tenant_rows
        assert await _list(client, tenant) == {rows["team"]}

    async def test_an_identity_ignores_it(self, client: AsyncClient, tenant_rows) -> None:
        tenant, rows = tenant_rows
        ids = await _list(client, tenant, caller_agent_id="alpha", include_scope_agent=True)
        assert ids == {rows["team"], rows["alpha_private"]}

    async def test_the_persons_counts_match_their_list(self, client: AsyncClient, tenant_rows) -> None:
        tenant, rows = tenant_rows
        stats = await client.post(
            f"{PREFIX}/memories/stats-breakdown",
            json={"tenant_id": tenant, "include_scope_agent": True},
        )
        assert stats.status_code == 200, stats.text
        assert stats.json()["total"] == len(rows)
        count = await client.get(f"{PREFIX}/memories/count-active", params={"tenant_id": tenant})
        assert count.status_code == 200, count.text
        assert count.json()["count"] == len(rows)
