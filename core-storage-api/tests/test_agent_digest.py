"""Integration tests for the agent-activity digest write/read path (Phase 2a).

Exercises ``POST/GET /reports/agent-activity`` against a live PostgreSQL,
focusing on the idempotent upsert over the two PARTIAL unique indexes
(fleet / no-fleet).
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PREFIX = "/api/v1/storage"
WIN_START = "2026-07-05T00:00:00+00:00"
WIN_END = "2026-07-06T00:00:00+00:00"


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _row(tenant: str, agent: str, *, fleet: str | None = None, **over) -> dict:
    body = {
        "run_id": str(uuid.uuid4()),
        "tenant_id": tenant,
        "fleet_id": fleet,
        "agent_id": agent,
        "period": "day",
        "window_start": WIN_START,
        "window_end": WIN_END,
        "narrative": "did some work",
        "sections": {"shipped": ["a thing"]},
        "source_count": 5,
        "recall_count": 2,
        "model": "gpt-5.4-mini",
        "status": "ok",
    }
    body.update(over)
    return body


async def _post(client: AsyncClient, body: dict):
    return await client.post(f"{PREFIX}/reports/agent-activity", json=body)


async def _latest(client: AsyncClient, tenant: str, **params) -> list[dict]:
    resp = await client.get(
        f"{PREFIX}/reports/agent-activity", params={"tenant_id": tenant, "period": "day", **params}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_upsert_is_idempotent_on_window_no_fleet(client: AsyncClient):
    """Re-running the same (tenant, agent, period, window) with fleet_id NULL
    replaces the row rather than duplicating it (no-fleet partial index)."""
    tenant, agent = f"dig-{_uid()}", "DevopsClaw"
    r1 = await _post(client, _row(tenant, agent, narrative="v1", source_count=3))
    assert r1.status_code == 200, r1.text

    r2 = await _post(client, _row(tenant, agent, narrative="v2", source_count=9))
    assert r2.status_code == 200, r2.text

    rows = await _latest(client, tenant)
    assert len(rows) == 1, rows
    assert rows[0]["narrative"] == "v2"
    assert rows[0]["source_count"] == 9


async def test_upsert_idempotent_with_fleet(client: AsyncClient):
    """Same natural key WITH a fleet_id upserts on the fleet partial index."""
    tenant, agent, fleet = f"dig-{_uid()}", "SecurityClaw", "etoro0"
    assert (await _post(client, _row(tenant, agent, fleet=fleet, narrative="a"))).status_code == 200
    assert (await _post(client, _row(tenant, agent, fleet=fleet, narrative="b"))).status_code == 200

    rows = await _latest(client, tenant)
    assert len(rows) == 1, rows
    assert rows[0]["narrative"] == "b"
    assert rows[0]["fleet_id"] == fleet


async def test_fleet_and_no_fleet_are_distinct_rows(client: AsyncClient):
    """A fleeted and a fleetless digest for the same agent/window are distinct —
    the two partial indexes don't collide."""
    tenant, agent = f"dig-{_uid()}", "BankingClaw"
    run = str(uuid.uuid4())
    await _post(client, _row(tenant, agent, fleet=None, run_id=run))
    await _post(client, _row(tenant, agent, fleet="etoro0", run_id=run))
    rows = await _latest(client, tenant)
    assert len(rows) == 2, rows


async def test_latest_run_returns_all_agents_sorted(client: AsyncClient):
    """One run's rows come back for all agents, ordered by source_count desc."""
    tenant = f"dig-{_uid()}"
    run = str(uuid.uuid4())
    await _post(client, _row(tenant, "low", run_id=run, source_count=1))
    await _post(client, _row(tenant, "high", run_id=run, source_count=42))
    rows = await _latest(client, tenant)
    assert [r["agent_id"] for r in rows] == ["high", "low"]


async def test_agent_id_filter(client: AsyncClient):
    tenant = f"dig-{_uid()}"
    run = str(uuid.uuid4())
    await _post(client, _row(tenant, "a", run_id=run))
    await _post(client, _row(tenant, "b", run_id=run))
    rows = await _latest(client, tenant, agent_id="a")
    assert len(rows) == 1 and rows[0]["agent_id"] == "a"


async def test_prune_deletes_rows_older_than_cutoff(client: AsyncClient):
    tenant = f"dig-{_uid()}"
    await _post(client, _row(tenant, "old", generated_at="2020-01-01T00:00:00+00:00"))
    await _post(client, _row(tenant, "new", generated_at="2026-07-06T00:00:00+00:00"))
    resp = await client.post(
        f"{PREFIX}/reports/agent-activity/prune",
        json={"tenant_id": tenant, "older_than": "2021-01-01T00:00:00+00:00"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == 1
    rows = await _latest(client, tenant)
    assert [r["agent_id"] for r in rows] == ["new"]


async def test_prune_validation(client: AsyncClient):
    resp = await client.post(
        f"{PREFIX}/reports/agent-activity/prune", json={"older_than": "2021-01-01T00:00:00+00:00"}
    )
    assert resp.status_code == 422 and "tenant_id" in resp.text


@pytest.mark.parametrize(
    "mutate,detail_needle",
    [
        ({"tenant_id": ""}, "tenant_id"),
        ({"status": None}, "status"),
        ({"period": "month"}, "day' or 'week"),
        ({"window_start": "not-a-date"}, "window_start"),
    ],
)
async def test_upsert_validation(client: AsyncClient, mutate: dict, detail_needle: str):
    body = _row(f"dig-{_uid()}", "a")
    body.update(mutate)
    resp = await _post(client, body)
    assert resp.status_code == 422, resp.text
    assert detail_needle in resp.text
