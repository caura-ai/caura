"""Expand step of #1095: ``POST /tenant-usage/query`` accepts a binding
singular ``tenant_id`` alongside the legacy ``tenant_ids``.

NOTHING IS FIXED YET. The plural still lets a caller name its own scope; it
is deleted in the contract step, after ``platform-admin-api`` has been
deployed on the singular field. These tests pin the expand contract only:
both spellings work, exactly one is required, and the two agree.
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
    """The binding singular field the contract step will make mandatory."""
    tenant, period = f"t-{_uid()}", _period()
    await _increment(
        client, [{"tenant_id": tenant, "operation": "write", "period_start": period, "count": 3}]
    )

    response = await client.post(f"{PREFIX}/tenant-usage/query", json={"tenant_id": tenant})

    assert response.status_code == 200, response.text
    assert response.json()["periods"][0]["operations"]["write"] == 3


async def test_singular_and_plural_agree_for_one_tenant(client: AsyncClient) -> None:
    """A singular request is the one-element case of the same aggregate, so the
    migrating caller can sum N responses rather than reshape them."""
    tenant, period = f"t-{_uid()}", _period()
    await _increment(
        client, [{"tenant_id": tenant, "operation": "search", "period_start": period, "count": 7}]
    )

    singular = await client.post(f"{PREFIX}/tenant-usage/query", json={"tenant_id": tenant})
    plural = await client.post(f"{PREFIX}/tenant-usage/query", json={"tenant_ids": [tenant]})

    assert singular.status_code == plural.status_code == 200
    assert singular.json() == plural.json()


async def test_plural_still_works_unchanged(client: AsyncClient) -> None:
    """Expand must not break the caller that has not migrated yet."""
    a, b, period = f"t-{_uid()}", f"t-{_uid()}", _period()
    await _increment(
        client,
        [
            {"tenant_id": a, "operation": "write", "period_start": period, "count": 2},
            {"tenant_id": b, "operation": "write", "period_start": period, "count": 5},
        ],
    )

    response = await client.post(f"{PREFIX}/tenant-usage/query", json={"tenant_ids": [a, b]})

    assert response.status_code == 200, response.text
    assert response.json()["periods"][0]["operations"]["write"] == 7


async def test_neither_scope_is_422(client: AsyncClient) -> None:
    """A caller that names no scope must never be read as 'every tenant'."""
    response = await client.post(f"{PREFIX}/tenant-usage/query", json={"periods": 3})

    assert response.status_code == 422, response.text


async def test_both_scopes_is_422(client: AsyncClient) -> None:
    """Ambiguous rather than harmless: silently preferring one would let a
    caller believe the other was honoured."""
    tenant = f"t-{_uid()}"
    response = await client.post(
        f"{PREFIX}/tenant-usage/query",
        json={"tenant_id": tenant, "tenant_ids": [tenant]},
    )

    assert response.status_code == 422, response.text


async def test_singular_scope_is_binding(client: AsyncClient) -> None:
    """The point of the singular field: it totals ONLY the tenant named, so a
    second tenant's counters cannot reach the response."""
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


async def test_period_filters_still_apply_to_the_singular_path(client: AsyncClient) -> None:
    """``period_start`` / ``periods`` are orthogonal to which scope field was
    used — the singular path must not quietly drop them."""
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
