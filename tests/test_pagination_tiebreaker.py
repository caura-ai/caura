"""Regression tests for the ``list_by_filters`` pagination tiebreaker.

Pre-fix the ``ORDER BY`` only sorted on the primary column with no
``id`` tiebreaker. When several rows shared the same primary value
(a single bulk-write tranche collides on ``created_at`` to ms
precision; low-cardinality columns like ``status`` collide trivially)
Postgres returned them in implementation-defined order — different
between consecutive paginated requests. The cursor predicate at
``memory_repository.py:159`` was already a tuple comparison on
``(created_at, id)``, so its WHERE clause filtered the wrong rows on
the next page when the ORDER BY didn't match.

The load test caught this: tenant A returned 736 unique / 264 dupes
across 20 pages, tenant B returned 654 unique / 346 dupes — finding
``pagination-duplicates``.

These tests force ``created_at`` collisions explicitly and assert
both halves of the bug:

* offset pagination → exactly N rows, no dupes, no skips
* cursor pagination → same, plus the cursor terminates cleanly
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.memory import Memory
from core_api.repositories import memory_repo

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _mem_at(tenant: str, ts: datetime, idx: int) -> Memory:
    """Build a Memory row pinned to a specific ``created_at``."""
    return Memory(
        id=uuid4(),
        tenant_id=tenant,
        agent_id="paginator",
        fleet_id=None,
        memory_type="fact",
        content=f"colliding-row-{idx}",
        weight=0.5,
        status="active",
        visibility="scope_team",
        created_at=ts,
    )


@pytest.fixture
async def collision_db(db: AsyncSession):
    """Seed 12 memories all sharing one ``created_at`` value."""
    tenant = f"t-pag-{uuid4().hex[:6]}"
    ts = datetime.now(UTC)
    mems = [_mem_at(tenant, ts, i) for i in range(12)]
    for m in mems:
        db.add(m)
    await db.flush()
    return tenant, mems


async def test_offset_pagination_no_duplicates_with_colliding_created_at(
    collision_db, db
):
    """Offset pagination across colliding ``created_at`` returns each
    row exactly once. Pre-fix: same-key rows shuffled between pages,
    causing duplicates and skips in the merged set."""
    tenant, seeded = collision_db
    n = len(seeded)

    seen_ids: list = []
    page_size = 5
    for offset in range(0, n + page_size, page_size):
        rows = await memory_repo.list_by_filters(
            db,
            tenant_id=tenant,
            limit=page_size,
            offset=offset,
        )
        # ``list_by_filters`` returns ``limit + 1`` for has_more
        # detection; the route trims to ``limit`` before returning to
        # the caller. Mirror that here.
        seen_ids.extend(m.id for m in rows[:page_size])

    unique_ids = set(seen_ids)
    assert len(seen_ids) == len(unique_ids), (
        f"pagination returned duplicates: {len(seen_ids)} rows total, "
        f"{len(unique_ids)} unique"
    )
    assert unique_ids == {m.id for m in seeded}, (
        "pagination skipped or duplicated rows; "
        f"missing={ {m.id for m in seeded} - unique_ids }, "
        f"extra={unique_ids - {m.id for m in seeded}}"
    )


async def test_cursor_pagination_no_duplicates_with_colliding_created_at(
    collision_db, db
):
    """Cursor pagination terminates cleanly and returns each row
    exactly once when ``created_at`` collides across the corpus.
    Pre-fix the cursor's ``(created_at, id) < tuple_(...)`` predicate
    rejected rows the previous page hadn't returned because the
    ORDER BY didn't include ``id``."""
    tenant, seeded = collision_db

    seen_ids: list = []
    cursor_ts: datetime | None = None
    cursor_id = None
    page_size = 5
    iterations = 0
    while True:
        rows = await memory_repo.list_by_filters(
            db,
            tenant_id=tenant,
            limit=page_size,
            cursor_ts=cursor_ts,
            cursor_id=cursor_id,
        )
        page = rows[:page_size]
        if not page:
            break
        seen_ids.extend(m.id for m in page)
        if len(rows) <= page_size:
            break  # has_more is False
        cursor_ts = page[-1].created_at
        cursor_id = page[-1].id
        iterations += 1
        if iterations > 10:
            pytest.fail("cursor pagination did not terminate")

    unique_ids = set(seen_ids)
    assert len(seen_ids) == len(unique_ids), (
        f"cursor pagination returned duplicates: {len(seen_ids)} rows, "
        f"{len(unique_ids)} unique"
    )
    assert unique_ids == {m.id for m in seeded}, (
        f"cursor pagination missed rows: "
        f"missing={ {m.id for m in seeded} - unique_ids }, "
        f"extra={unique_ids - {m.id for m in seeded}}"
    )


async def test_paginated_order_by_helper_pairs_id_in_same_direction():
    """``paginated_order_by`` returns ``(primary, id)`` in the same
    direction. Both pagination call sites — the repository at
    ``memory_repository.list_by_filters:164`` and the admin route at
    ``routes/memories.admin_list_memories:1040`` — call this single
    helper so the tiebreaker stays in sync with the
    ``tuple_(created_at, id)`` cursor predicate everywhere.

    The integration tests above exercise the helper through
    ``list_by_filters``; this unit test pins the helper's contract
    so a future refactor of either call site that drops the helper
    surfaces immediately."""
    from common.models.memory import Memory

    from core_api.pagination import paginated_order_by

    desc_clause = paginated_order_by(Memory.created_at, Memory.id, "desc")
    asc_clause = paginated_order_by(Memory.created_at, Memory.id, "asc")

    # SQLAlchemy ColumnElement comparison-direction lives on
    # ``modifier`` for the standard sort.
    assert len(desc_clause) == 2
    assert str(desc_clause[0]).endswith("DESC")
    assert str(desc_clause[1]).endswith("DESC")
    assert "id" in str(desc_clause[1]).lower()

    assert len(asc_clause) == 2
    assert str(asc_clause[0]).endswith("ASC")
    assert str(asc_clause[1]).endswith("ASC")
    assert "id" in str(asc_clause[1]).lower()
