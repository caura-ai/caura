"""OSS audit 09/02 M-60 — ``expires_at`` was accepted, stored, returned, never enforced.

The field is settable on every write schema, echoed on reads, and offered as a
sort key. Nothing ever acted on it.

What made it read as "never enforced" rather than "enforced late" is that an
expiry sweep DOES exist — ``memory_archive_expired``, wired through
``run_archive_expired_tick`` into the core-worker lifecycle — and it filtered
``ts_valid_end``, a DIFFERENT column that happens to sit beside it on the model.
So the machinery that looked like it enforced expiry enforced something else.

The decision (caura#1637) was to extend the sweep rather than add a read-time
filter, and to document the weaker guarantee that follows: a row is archived on
the next tick, not hidden at the instant it expires.

Measured in production before the change: of 2,099,319 memories, **zero** had
``expires_at`` set. So this enforces nothing retroactively — it makes the field
honest for the first caller who ever uses it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = pytest.mark.asyncio


async def _seed(
    tenant: str,
    *,
    expires_at: datetime | None = None,
    ts_valid_end: datetime | None = None,
    status: str = "active",
) -> uuid.UUID:
    memory_id = uuid.uuid4()
    async with get_session() as session:
        await session.execute(
            text(
                "INSERT INTO memories "
                "(id, tenant_id, agent_id, memory_type, content, status, "
                " expires_at, ts_valid_end) "
                "VALUES (:id, :t, 'm60-probe', 'semantic', 'probe', :status, "
                " :expires_at, :ts_valid_end)"
            ),
            {
                "id": memory_id,
                "t": tenant,
                "status": status,
                "expires_at": expires_at,
                "ts_valid_end": ts_valid_end,
            },
        )
    return memory_id


async def _status(memory_id: uuid.UUID) -> str:
    async with get_session() as session:
        result = await session.execute(text("SELECT status FROM memories WHERE id = :id"), {"id": memory_id})
        return result.scalar_one()


async def test_a_past_expires_at_is_archived_by_the_sweep(_ensure_schema) -> None:
    """The finding, directly: this row was served forever before the change."""
    tenant = f"m60-{uuid.uuid4().hex[:12]}"
    past = await _seed(tenant, expires_at=datetime.now(UTC) - timedelta(hours=1))

    swept = await PostgresService().memory_archive_expired(tenant_id=tenant)

    assert swept == 1
    assert await _status(past) == "outdated"


async def test_a_future_expires_at_is_left_alone(_ensure_schema) -> None:
    """Guards the obvious over-correction — sweeping on the column's presence
    rather than on its value would archive every row a caller ever set it on."""
    tenant = f"m60-{uuid.uuid4().hex[:12]}"
    future = await _seed(tenant, expires_at=datetime.now(UTC) + timedelta(days=30))

    swept = await PostgresService().memory_archive_expired(tenant_id=tenant)

    assert swept == 0
    assert await _status(future) == "active"


async def test_ts_valid_end_still_drives_the_sweep(_ensure_schema) -> None:
    """The behaviour that already existed. Without this the change could pass by
    REPLACING one column with the other rather than covering both."""
    tenant = f"m60-{uuid.uuid4().hex[:12]}"
    closed = await _seed(tenant, ts_valid_end=datetime.now(UTC) - timedelta(hours=1))

    swept = await PostgresService().memory_archive_expired(tenant_id=tenant)

    assert swept == 1
    assert await _status(closed) == "outdated"


async def test_a_row_with_neither_column_set_is_untouched(_ensure_schema) -> None:
    """NULL semantics: ``NULL < NOW()`` is NULL, and ``NULL OR NULL`` is NULL,
    so a row carrying neither timestamp must not match the widened predicate."""
    tenant = f"m60-{uuid.uuid4().hex[:12]}"
    plain = await _seed(tenant)

    swept = await PostgresService().memory_archive_expired(tenant_id=tenant)

    assert swept == 0
    assert await _status(plain) == "active"


async def test_the_sweep_does_not_resurrect_or_re_archive(_ensure_schema) -> None:
    """Only ``active`` rows are candidates, so a second tick is a no-op rather
    than a rewrite — and a row already archived by another path stays as it is."""
    tenant = f"m60-{uuid.uuid4().hex[:12]}"
    await _seed(tenant, expires_at=datetime.now(UTC) - timedelta(hours=1))
    already = await _seed(tenant, expires_at=datetime.now(UTC) - timedelta(hours=1), status="archived")

    first = await PostgresService().memory_archive_expired(tenant_id=tenant)
    second = await PostgresService().memory_archive_expired(tenant_id=tenant)

    assert first == 1
    assert second == 0
    assert await _status(already) == "archived"
