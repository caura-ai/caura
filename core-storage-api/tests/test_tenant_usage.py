"""The read half of ``tenant_usage_counters``, against a live PostgreSQL.

caura-ai/caura-enterprise#83. The platform bills an ORGANISATION; the meter
only ever knows a tenant. So the query this covers is "total these tenants,
per period, per operation" — and the properties worth pinning are the ones a
plausible simpler query gets wrong:

* the sum must span the org's tenants, not report one of them,
* the operation names must survive, including the ones with no column in the
  platform's writes/searches/recalls shape,
* ``periods=N`` must mean N periods, not N rows — a period holds as many rows
  as it has operations, so a plain LIMIT would return half of one period.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

PREFIX = "/api/v1/storage"

JUNE = "2026-06-01T00:00:00+00:00"
JULY = "2026-07-01T00:00:00+00:00"
AUGUST = "2026-08-01T00:00:00+00:00"


def _uid() -> str:
    return uuid.uuid4().hex[:8]


async def _increment(client: AsyncClient, rows: list[dict]) -> None:
    r = await client.post(f"{PREFIX}/tenant-usage/increment", json={"rows": rows})
    assert r.status_code == 200, r.text


async def _query(client: AsyncClient, **body) -> dict:
    r = await client.post(f"{PREFIX}/tenant-usage/query", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _row(tenant: str, operation: str, period: str, count: int) -> dict:
    return {
        "tenant_id": tenant,
        "operation": operation,
        "period_start": period,
        "count": count,
    }


async def test_counts_sum_across_the_orgs_tenants(client: AsyncClient):
    """An org owns several tenants and is billed as one. Reporting a single
    tenant's counts would under-bill by however many tenants it has."""
    a, b = f"t-{_uid()}", f"t-{_uid()}"
    await _increment(
        client,
        [
            _row(a, "write", AUGUST, 5),
            _row(b, "write", AUGUST, 7),
            _row(b, "search", AUGUST, 2),
        ],
    )

    got = await _query(client, tenant_ids=[a, b], period_start=AUGUST)

    assert got["periods"] == [{"period_start": AUGUST, "operations": {"write": 12, "search": 2}}]


async def test_a_tenant_outside_the_org_is_not_counted(client: AsyncClient):
    a, outsider = f"t-{_uid()}", f"t-{_uid()}"
    await _increment(
        client,
        [_row(a, "write", AUGUST, 3), _row(outsider, "write", AUGUST, 100)],
    )

    got = await _query(client, tenant_ids=[a], period_start=AUGUST)

    assert got["periods"][0]["operations"] == {"write": 3}


async def test_operations_without_a_platform_column_survive(client: AsyncClient):
    """``insights`` and ``evolve`` are metered by core-api but have no column
    in the platform's writes/searches/recalls triple. Mapping them away here
    would discard counts the write path already paid for."""
    t = f"t-{_uid()}"
    await _increment(
        client,
        [
            _row(t, "write", AUGUST, 1),
            _row(t, "insights", AUGUST, 4),
            _row(t, "evolve", AUGUST, 2),
        ],
    )

    got = await _query(client, tenant_ids=[t], period_start=AUGUST)

    assert got["periods"][0]["operations"] == {"write": 1, "insights": 4, "evolve": 2}


async def test_periods_means_periods_not_rows(client: AsyncClient):
    """Regression guard for the obvious simplification.

    A plain ``LIMIT periods`` on the grouped rows cuts mid-period, because one
    period contributes one row per operation. August alone has three here, so
    ``periods=2`` under a row-limit would return August twice and never reach
    July.
    """
    t = f"t-{_uid()}"
    await _increment(
        client,
        [
            _row(t, "write", AUGUST, 1),
            _row(t, "search", AUGUST, 1),
            _row(t, "insights", AUGUST, 1),
            _row(t, "write", JULY, 9),
            _row(t, "write", JUNE, 4),
        ],
    )

    got = await _query(client, tenant_ids=[t], periods=2)

    assert [p["period_start"] for p in got["periods"]] == [AUGUST, JULY]
    assert got["periods"][1]["operations"] == {"write": 9}


async def test_quiet_periods_do_not_consume_the_window(client: AsyncClient):
    """``periods`` counts periods that have data. A tenant idle in July should
    still get June back, rather than spending the slot on an empty month."""
    t = f"t-{_uid()}"
    await _increment(client, [_row(t, "write", AUGUST, 1), _row(t, "write", JUNE, 2)])

    got = await _query(client, tenant_ids=[t], periods=2)

    assert [p["period_start"] for p in got["periods"]] == [AUGUST, JUNE]


async def test_an_unknown_tenant_reads_empty_rather_than_404(client: AsyncClient):
    """A brand-new org has no counters yet, and that is not an error — the
    dashboard shows zeros. The legacy endpoint 404'd on this."""
    assert await _query(client, tenant_ids=[f"t-{_uid()}"]) == {"periods": []}


async def test_an_empty_tenant_list_is_rejected(client: AsyncClient):
    """An org with no tenants must not read as 'total every tenant'."""
    r = await client.post(f"{PREFIX}/tenant-usage/query", json={"tenant_ids": []})
    assert r.status_code == 422
