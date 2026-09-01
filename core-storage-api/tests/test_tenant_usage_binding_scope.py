"""``POST /tenant-usage/query`` binds to one tenant (#1095).

The plural ``tenant_ids`` is gone as of the contract step: it let a caller hand
this service the list of tenants to total, and since #1066 the only credential
here is a shared secret carrying no tenant identity, so nothing could check
that list against who sent it. What remains is pinned here — the singular field
is required, non-empty, and genuinely confines the query.

The expand-era cases that asserted the two spellings coexisted (``…agree for
one tenant``, ``plural still works unchanged``, ``both scopes is 422``) are
deliberately gone with the field they tested.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = pytest.mark.asyncio


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _period() -> str:
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()


async def _increment(client: AsyncClient, rows: list[dict]) -> None:
    response = await client.post(f"{PREFIX}/tenant-usage/increment", json={"rows": rows})
    assert response.status_code == 200, response.text


async def test_singular_tenant_id_is_accepted(client: AsyncClient) -> None:
    """The binding field, and now the only one."""
    tenant, period = f"t-{_uid()}", _period()
    await _increment(
        client, [{"tenant_id": tenant, "operation": "write", "period_start": period, "count": 3}]
    )

    response = await client.post(f"{PREFIX}/tenant-usage/query", json={"tenant_id": tenant})

    assert response.status_code == 200, response.text
    assert response.json()["periods"][0]["operations"]["write"] == 3


async def test_no_scope_is_422(client: AsyncClient) -> None:
    """A caller that names no scope must never be read as 'every tenant'."""
    response = await client.post(f"{PREFIX}/tenant-usage/query", json={"periods": 3})

    assert response.status_code == 422, response.text


async def test_singular_scope_is_binding(client: AsyncClient) -> None:
    """The whole point: the query totals ONLY the tenant named, so a second
    tenant's counters cannot reach the response."""
    mine, theirs, period = f"t-{_uid()}", f"t-{_uid()}", _period()
    await _increment(
        client,
        [
            {"tenant_id": mine, "operation": "write", "period_start": period, "count": 1},
            {"tenant_id": theirs, "operation": "write", "period_start": period, "count": 99},
        ],
    )

    response = await client.post(f"{PREFIX}/tenant-usage/query", json={"tenant_id": mine})

    assert response.status_code == 200, response.text
    assert response.json()["periods"][0]["operations"]["write"] == 1


async def test_period_filters_still_apply(client: AsyncClient) -> None:
    """``period_start`` / ``periods`` are orthogonal to the scope field and
    must not have been dropped when the plural went."""
    tenant = f"t-{_uid()}"
    now = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    older = (now - timedelta(days=60)).replace(day=1).isoformat()
    await _increment(
        client,
        [
            {"tenant_id": tenant, "operation": "write", "period_start": now.isoformat(), "count": 4},
            {"tenant_id": tenant, "operation": "write", "period_start": older, "count": 8},
        ],
    )

    response = await client.post(
        f"{PREFIX}/tenant-usage/query",
        json={"tenant_id": tenant, "period_start": older},
    )

    assert response.status_code == 200, response.text
    periods = response.json()["periods"]
    assert len(periods) == 1
    assert periods[0]["operations"]["write"] == 8
