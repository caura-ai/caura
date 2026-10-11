"""Rows of one bulk write get strictly increasing ``created_at`` in batch order.

A single multi-row INSERT gave every row the same ``now()``. Contradiction
detection decides which of two memories is older by ``created_at`` and, on a
tie, by random UUID order, so an "update" written in the same batch as the
fact it corrects was marked stale about half the time.
"""

from __future__ import annotations

from itertools import pairwise
from uuid import uuid4

import pytest
from sqlalchemy import select

from common.models import Memory
from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_svc = PostgresService()


def _item(tenant: str, i: int) -> dict:
    return {
        "tenant_id": tenant,
        "fleet_id": None,
        "agent_id": "bulk-order",
        "client_request_id": f"bulk-order-{i}-{uuid4().hex[:6]}",
        "content": f"bulk order fact {i}",
        "memory_type": "fact",
        "weight": 0.5,
        "status": "active",
        "visibility": "scope_team",
    }


async def test_bulk_rows_are_ordered_by_created_at_in_input_order():
    tenant = f"test-tenant-bulkorder-{uuid4().hex[:8]}"
    results = await _svc.memory_add_all([_item(tenant, i) for i in range(5)])
    ids = [r["id"] for r in results]
    async with get_session() as s:
        rows = {
            str(r.id): r.created_at
            for r in (
                await s.execute(select(Memory).where(Memory.tenant_id == tenant))
            ).scalars()
        }
    stamps = [rows[str(i)] for i in ids]
    assert all(a < b for a, b in pairwise(stamps)), stamps
    assert (stamps[-1] - stamps[0]).total_seconds() < 0.01


async def test_a_caller_supplied_created_at_is_kept():
    from datetime import UTC, datetime

    tenant = f"test-tenant-bulkorder-{uuid4().hex[:8]}"
    when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    items = [{**_item(tenant, i), "created_at": when} for i in range(2)]
    results = await _svc.memory_add_all(items)
    async with get_session() as s:
        got = (
            await s.execute(
                select(Memory.created_at).where(Memory.id == results[0]["id"])
            )
        ).scalar_one()
    assert got == when
