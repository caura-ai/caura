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


class TestIncrementRequiresATenantPerRow:
    """The write half's per-row guard, which is what its classification rests on.

    ``POST /tenant-usage/increment`` is filed ``opaque-body-write`` in
    ``tenant_scope_allowlist.json``: the batch is cross-tenant by design, so
    there is no binding tenant parameter and the scope rides in each row
    instead. That category's whole claim is *"the binding is real but lives in
    the row being written"* — which holds only while a row without a
    ``tenant_id`` is refused.

    Nothing pinned that. The rest of this file covers the read half, so the
    guard could have been deleted and the only signal would have been the
    allowlist note quietly becoming false. These two cases are the classification's
    premise, written down so it fails loudly instead.
    """

    async def test_a_row_without_a_tenant_id_is_refused(self, client: AsyncClient):
        tenant, operation = f"t-{_uid()}", f"op-{_uid()}"
        r = await client.post(
            f"{PREFIX}/tenant-usage/increment",
            json={
                "rows": [
                    _row(tenant, operation, AUGUST, 1),
                    {"operation": operation, "period_start": AUGUST, "count": 5},
                ]
            },
        )

        assert r.status_code == 422, r.text
        # The offending row is named. ``_require`` would drop this index — see
        # the rejected alternative in this PR's description.
        assert "row 1" in r.text
        assert "tenant_id" in r.text
        # The whole batch is refused, so the well-formed row in it did not land
        # either: the guard runs over every row before any write is attempted.
        assert await _query(client, tenant_id=tenant, period_start=AUGUST) == {"periods": []}

    async def test_a_row_without_an_operation_is_refused(self, client: AsyncClient):
        tenant = f"t-{_uid()}"

        r = await client.post(
            f"{PREFIX}/tenant-usage/increment",
            json={"rows": [{"tenant_id": tenant, "period_start": AUGUST, "count": 5}]},
        )

        assert r.status_code == 422, r.text
        assert "row 0" in r.text
        assert await _query(client, tenant_id=tenant, period_start=AUGUST) == {"periods": []}


class TestTheCounterCannotBeDrivenNegative:
    """A negative ``count`` is a plan-enforcement bypass, not a small number.

    The upsert ADDS (``count = count + excluded.count``), so a negative in the
    body decrements the stored total. The platform then asks ``used > limit``,
    which a counter below zero answers "no" for any limit — enforcement stops
    for that tenant and nothing says so. These pin the two places that is now
    refused: the router, and the table underneath it.
    """

    async def test_a_negative_count_is_refused(self, client: AsyncClient):
        tenant, operation = f"t-{_uid()}", f"op-{_uid()}"
        await _increment(client, [_row(tenant, operation, AUGUST, 10)])

        r = await client.post(
            f"{PREFIX}/tenant-usage/increment",
            json={"rows": [_row(tenant, operation, AUGUST, -50)]},
        )

        assert r.status_code == 422, r.text
        assert "row 0" in r.text
        assert "count" in r.text
        # The established total is untouched — the batch was refused before any
        # write, so the decrement never reached the upsert.
        got = await _query(client, tenant_id=tenant, period_start=AUGUST)
        assert got["periods"][0]["operations"][operation] == 10

    async def test_a_boolean_count_is_refused(self, client: AsyncClient):
        """``bool`` is an ``int`` in Python, so a JSON ``true`` would otherwise
        meter exactly 1 operation by accident rather than by request."""
        tenant, operation = f"t-{_uid()}", f"op-{_uid()}"

        r = await client.post(
            f"{PREFIX}/tenant-usage/increment",
            json={"rows": [{**_row(tenant, operation, AUGUST, 1), "count": True}]},
        )

        assert r.status_code == 422, r.text
        assert await _query(client, tenant_id=tenant, period_start=AUGUST) == {"periods": []}

    async def test_a_zero_count_is_still_accepted(self, client: AsyncClient):
        """The boundary the guard must not over-reach: zero is a legitimate
        no-op flush, and refusing it would break the meter rather than protect
        it."""
        tenant, operation = f"t-{_uid()}", f"op-{_uid()}"

        await _increment(client, [_row(tenant, operation, AUGUST, 0)])

        got = await _query(client, tenant_id=tenant, period_start=AUGUST)
        assert got["periods"][0]["operations"][operation] == 0

    async def test_the_table_refuses_a_negative_count_beneath_the_router(self):
        """The floor under the edge check, covering what it cannot see.

        The router only guards the request path. The way this gap was actually
        found was a correction run by hand against the table, which reaches the
        column directly — so the constraint is tested directly too, not through
        the endpoint that would reject the row before it got there.
        """
        from datetime import datetime

        from sqlalchemy import text as _text
        from sqlalchemy.exc import IntegrityError

        from core_storage_api.services.postgres_service import get_session

        with pytest.raises(IntegrityError, match="ck_tenant_usage_counters_count_nonneg"):
            async with get_session() as session:
                await session.execute(
                    _text(
                        "INSERT INTO tenant_usage_counters "
                        "(tenant_id, operation, period_start, count) "
                        "VALUES (:t, :o, :p, -1)"
                    ),
                    # A real ``datetime``: asyncpg rejects a str bound to a
                    # timestamptz parameter outright rather than coercing it,
                    # which is the same reason the router parses
                    # ``period_start`` before it reaches the insert.
                    {
                        "t": f"t-{_uid()}",
                        "o": f"op-{_uid()}",
                        "p": datetime.fromisoformat(AUGUST),
                    },
                )


# ``test_counts_sum_across_the_orgs_tenants`` lived here and is deliberately
# gone (#1095, contract step). Summing an ORG's tenants was the capability the
# plural ``tenant_ids`` existed for, and it moved to ``platform-admin-api``,
# which owns the org->tenant mapping this service cannot see. Its replacement is
# ``test_totals_are_summed_across_tenants`` in
# ``platform-admin-api/tests/test_core_storage_client_usage_fanout.py``. Storage
# answers for one tenant now, and that is the point rather than a regression.


async def test_a_tenant_outside_the_org_is_not_counted(client: AsyncClient):
    a, outsider = f"t-{_uid()}", f"t-{_uid()}"
    await _increment(
        client,
        [_row(a, "write", AUGUST, 3), _row(outsider, "write", AUGUST, 100)],
    )

    got = await _query(client, tenant_id=a, period_start=AUGUST)

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

    got = await _query(client, tenant_id=t, period_start=AUGUST)

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

    got = await _query(client, tenant_id=t, periods=2)

    assert [p["period_start"] for p in got["periods"]] == [AUGUST, JULY]
    assert got["periods"][1]["operations"] == {"write": 9}


async def test_quiet_periods_do_not_consume_the_window(client: AsyncClient):
    """``periods`` counts periods that have data. A tenant idle in July should
    still get June back, rather than spending the slot on an empty month."""
    t = f"t-{_uid()}"
    await _increment(client, [_row(t, "write", AUGUST, 1), _row(t, "write", JUNE, 2)])

    got = await _query(client, tenant_id=t, periods=2)

    assert [p["period_start"] for p in got["periods"]] == [AUGUST, JUNE]


async def test_an_unknown_tenant_reads_empty_rather_than_404(client: AsyncClient):
    """A brand-new org has no counters yet, and that is not an error — the
    dashboard shows zeros. The legacy endpoint 404'd on this."""
    assert await _query(client, tenant_id=f"t-{_uid()}") == {"periods": []}


async def test_a_missing_tenant_id_is_rejected(client: AsyncClient):
    """Was ``test_an_empty_tenant_list_is_rejected``. The plural is gone, so
    the shape of "named no scope" changed from ``[]`` to an absent field — but
    the requirement is the same one, and it is the requirement that matters:
    an unscoped query must never be read as "every tenant"."""
    r = await client.post(f"{PREFIX}/tenant-usage/query", json={})
    assert r.status_code == 422, r.text


async def test_an_empty_tenant_id_is_rejected(client: AsyncClient):
    """``min_length=1`` — an empty string is a caller that lost its scope, not
    a caller asking about a tenant named ""."""
    r = await client.post(f"{PREFIX}/tenant-usage/query", json={"tenant_id": ""})
    assert r.status_code == 422, r.text


async def test_the_plural_field_is_gone(client: AsyncClient):
    """The defect #1095 closes: a caller can no longer hand this service the
    list of tenants to total. Sending only the old field is now an unscoped
    request and must be refused, not silently honoured."""
    r = await client.post(f"{PREFIX}/tenant-usage/query", json={"tenant_ids": [f"t-{_uid()}"]})
    assert r.status_code == 422, r.text
