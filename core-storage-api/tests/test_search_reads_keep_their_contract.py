"""Storage's search reads answer the way the scored search does (audit 2026-10-01, B33).

The storage half of the batch's search-correctness PR. Each case here failed on
main:

- the link lookup behind entity search returned every link of every entity in
  no order, deleted memories included, so a hub entity's capped pool was
  whatever the planner returned first (L-13, L-195);
- the successor lookup compared ``valid_at`` to the second and as a hard window
  on both ends, and read the home tenant only, where the search it corrects
  compares by day, treats an end date softly and reads every readable tenant
  (L-43);
- a ``valid_at`` with an offset was compared by its own calendar day, not the
  UTC one, and the row side was cast to a date in the session's time zone
  (L-146).
"""

from __future__ import annotations

import inspect
import re
import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from core_storage_api.database.init import get_engine
from core_storage_api.services import postgres_service
from tests.test_integration import PREFIX, _memory_payload

pytestmark = [pytest.mark.integration]


def _tenant() -> str:
    """A fresh tenant, under the prefix the end-of-run sweep removes."""
    return f"test-tenant-{uuid.uuid4().hex[:8]}"


async def _memory(client: AsyncClient, tenant: str, **fields: str) -> str:
    payload = {**_memory_payload(tenant, "f1"), **fields}
    resp = await client.post(f"{PREFIX}/memories", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _set(table: str, row_id: str, column: str, value: datetime) -> None:
    async with get_engine().begin() as conn:
        await conn.execute(
            text(f"UPDATE {table} SET {column} = :v WHERE id = :id"), {"v": value, "id": uuid.UUID(row_id)}
        )


async def _status(client: AsyncClient, tenant: str, memory_id: str, status: str, **edge: str) -> None:
    resp = await client.patch(
        f"{PREFIX}/memories/{memory_id}/status", json={"tenant_id": tenant, "status": status, **edge}
    )
    assert resp.status_code == 200, resp.text


async def _successors(client: AsyncClient, tenant: str, loser: str, **scope) -> list[str]:
    resp = await client.post(
        f"{PREFIX}/memories/find-successors",
        json={"supersedes_ids": [loser], "tenant_id": tenant, **scope},
    )
    assert resp.status_code == 200, resp.text
    return [row["id"] for row in resp.json()]


async def _superseded(client: AsyncClient, tenant: str, **winner_fields: str) -> tuple[str, str]:
    """``(loser, winner)``: the winner's ``supersedes_id`` names the loser."""
    loser = await _memory(client, tenant, ts_valid_start="2026-03-01T08:00:00+00:00")
    winner = await _memory(client, tenant, **winner_fields)
    await _status(client, tenant, winner, "active", supersedes_id=loser)
    await _status(client, tenant, loser, "outdated")
    return loser, winner


# ── L-13, L-195: a hub entity's links come newest first, live and bounded ────


async def test_l13_l195_links_come_newest_live_first_and_capped(client: AsyncClient, monkeypatch) -> None:
    tenant = _tenant()
    resp = await client.post(
        f"{PREFIX}/entities",
        json={"tenant_id": tenant, "entity_type": "person", "canonical_name": f"Hub {uuid.uuid4().hex[:8]}"},
    )
    assert resp.status_code == 200, resp.text
    entity = resp.json()["id"]

    async def linked(created: datetime) -> str:
        mid = await _memory(client, tenant)
        await _set("memories", mid, "created_at", created)
        resp = await client.post(
            f"{PREFIX}/entities/links",
            json={"tenant_id": tenant, "memory_id": mid, "entity_id": entity, "role": "subject"},
        )
        assert resp.status_code == 200, resp.text
        return mid

    # Created out of order, so insertion order cannot pass for recency.
    by_day = {day: await linked(datetime(2026, 1, day, tzinfo=UTC)) for day in (5, 1, 4, 2, 3)}
    gone = await linked(datetime(2026, 1, 9, tzinfo=UTC))
    await _set("memories", gone, "deleted_at", datetime(2026, 1, 10, tzinfo=UTC))
    # Read when the lookup runs, so a small cap stands in for the real one.
    monkeypatch.setattr(postgres_service, "MEMORY_LINKS_PER_ENTITY", 3, raising=False)

    resp = await client.post(
        f"{PREFIX}/entities/memory-ids-by-entity-ids", json={"tenant_id": tenant, "entity_ids": [entity]}
    )

    assert resp.status_code == 200, resp.text
    assert [row["memory_id"] for row in resp.json()] == [by_day[5], by_day[4], by_day[3]]


# ── L-43: a successor is found by the rules the search used ─────────────────


async def test_l43_a_successor_written_later_the_same_day_is_found(client: AsyncClient) -> None:
    """Scored search admits a row valid from the same day as ``valid_at``; the
    lookup for its correction compared to the second and missed it."""
    tenant = _tenant()
    loser, winner = await _superseded(client, tenant, ts_valid_start="2026-03-01T18:00:00+00:00")

    assert await _successors(client, tenant, loser, valid_at="2026-03-01T09:00:00+00:00") == [winner]


async def test_l43_a_successor_whose_end_date_has_passed_is_still_found(client: AsyncClient) -> None:
    """A past ``ts_valid_end`` only discounts a row in scored search, so an
    enrichment date cannot hide it; here it hid the correction."""
    tenant = _tenant()
    loser, winner = await _superseded(
        client,
        tenant,
        ts_valid_start="2026-01-01T00:00:00+00:00",
        ts_valid_end="2026-02-01T00:00:00+00:00",
    )

    assert await _successors(client, tenant, loser, valid_at="2026-03-01T09:00:00+00:00") == [winner]


async def test_l43_a_cross_tenant_read_finds_successors_where_it_read(client: AsyncClient) -> None:
    home, other = _tenant(), _tenant()
    loser, winner = await _superseded(client, other)

    assert await _successors(client, home, loser, readable_tenant_ids=[home, other]) == [winner]


# ── L-146: valid_at is compared as a UTC day ────────────────────────────────


async def test_l146_an_offset_valid_at_is_compared_by_its_utc_day(client: AsyncClient) -> None:
    """01:00 at +05:00 on 2 January is 20:00 UTC on 1 January, so a memory valid
    from 00:30 UTC on 2 January had not started; it was compared to 2 January."""
    tenant = _tenant()
    mid = await _memory(client, tenant, ts_valid_start="2026-01-02T00:30:00+00:00")

    resp = await client.post(
        f"{PREFIX}/memories/load-by-ids",
        json={"tenant_id": tenant, "memory_ids": [mid], "valid_at": "2026-01-02T01:00:00+05:00"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_l146_no_timestamp_is_cast_to_a_date_in_the_session_time_zone() -> None:
    """``timestamptz::date`` takes the day in the session's ``TimeZone``, which
    nothing pins, so on a non-UTC database the row's day and the caller's UTC
    day were taken in two zones. ``memory_daily_durable_counts`` already reads
    ``timezone('UTC', ...)`` for this reason; the search reads now do too."""
    source = inspect.getsource(postgres_service)

    assert re.findall(r"cast\((?:Memory|ing\.c)\.\w+, _?\w*Date\)", source) == []
