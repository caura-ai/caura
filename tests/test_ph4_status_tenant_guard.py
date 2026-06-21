"""Ph4 PR #432 round-3 review fixes — storage-layer regression tests.

Two findings exercised here, both at the storage service / typed-client level
(bridged in-process to the storage app by the conftest ASGI fixture, against the
test DB):

- FIX 1 (HIGH): ``memory_update_status`` now requires a ``tenant_id`` and scopes
  its UPDATE to ``Memory.tenant_id == tenant_id``. A caller in tenant B must not
  be able to flip the status of tenant A's memory by id (cross-tenant write gap).
- FIX 2 (MED): ``memory_list_by_filters`` cursor predicate now branches on
  ``order`` — an ``order="asc"`` page must walk FORWARD (toward newer rows), not
  backward.

Rows are seeded directly via the storage write path (committed, independent
session) with explicit ``created_at`` so the cursor ordering is deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from core_storage_api.services.postgres_service import PostgresService

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _t() -> str:
    """Unique tenant id per test so concurrent suite runs don't collide."""
    return f"test-tenant-ph4guard-{uuid4().hex[:8]}"


async def _add_memory(
    svc: PostgresService,
    tenant: str,
    *,
    content: str = "fact",
    status: str = "active",
    created_at: datetime | None = None,
) -> str:
    data: dict = {
        "tenant_id": tenant,
        "agent_id": "test-agent",
        "memory_type": "fact",
        "content": f"{content} [{uuid4().hex[:8]}]",
        "status": status,
    }
    if created_at is not None:
        data["created_at"] = created_at
    mem = await svc.memory_add(data)
    return str(mem.id)


# ---------------------------------------------------------------------------
# FIX 1 — cross-tenant write guard on memory_update_status
# ---------------------------------------------------------------------------


async def test_update_status_wrong_tenant_is_a_noop():
    """A status update for a memory in tenant A, called with the WRONG tenant,
    must not touch the row (rowcount 0 → returns False)."""
    svc = PostgresService()
    tenant_a = _t()
    tenant_b = _t()
    mid = await _add_memory(svc, tenant_a, status="active")

    # Wrong tenant → guarded out, no row updated.
    updated = await svc.memory_update_status(UUID(mid), "archived", tenant_id=tenant_b)
    assert updated is False

    # Row is untouched: still active.
    rows = await svc.memory_list_by_filters(tenant_id=tenant_a, include_deleted=True)
    by_id = {str(m.id): m for m in rows}
    assert by_id[mid].status == "active"


async def test_update_status_correct_tenant_updates():
    """The same call with the correct (home) tenant updates the row."""
    svc = PostgresService()
    tenant_a = _t()
    mid = await _add_memory(svc, tenant_a, status="active")

    updated = await svc.memory_update_status(UUID(mid), "archived", tenant_id=tenant_a)
    assert updated is True

    rows = await svc.memory_list_by_filters(tenant_id=tenant_a, include_deleted=True)
    by_id = {str(m.id): m for m in rows}
    assert by_id[mid].status == "archived"


async def test_route_status_update_cross_tenant_finds_no_row(sc):
    """End-to-end through the typed client + PATCH route: a cross-tenant status
    update matches no row (guarded WHERE) → the route 404s, which the client's
    ``_patch`` helper translates to ``None`` (same convention as a nonexistent
    id). The row's status is unchanged. The correct tenant then succeeds.
    Confirms the full tenant_id thread (client → route → service)."""
    svc = PostgresService()
    tenant_a = _t()
    tenant_b = _t()
    mid = await _add_memory(svc, tenant_a, status="active")

    # Wrong tenant: guarded out → 404 → None, and the row is untouched.
    result = await sc.update_memory_status(mid, "archived", tenant_id=tenant_b)
    assert result is None
    untouched = await sc.get_memory(mid)
    assert untouched["status"] == "active"

    # Correct tenant succeeds.
    result = await sc.update_memory_status(mid, "archived", tenant_id=tenant_a)
    assert result is not None
    post = await sc.get_memory(mid)
    assert post["status"] == "archived"


# ---------------------------------------------------------------------------
# FIX 2 — cursor direction respects order="asc"
# ---------------------------------------------------------------------------


async def test_list_by_filters_asc_cursor_returns_forward_page():
    """``order="asc"`` + a cursor must return the FORWARD page (rows NEWER than
    the cursor). Pre-fix the predicate hardcoded ``<`` and paginated backward,
    returning rows older than the cursor (an empty / wrong page)."""
    svc = PostgresService()
    tenant = _t()
    base = datetime.now(UTC) - timedelta(hours=1)
    # Three rows, strictly increasing created_at.
    m1 = await _add_memory(svc, tenant, content="oldest", created_at=base)
    m2 = await _add_memory(
        svc, tenant, content="middle", created_at=base + timedelta(minutes=10)
    )
    m3 = await _add_memory(
        svc, tenant, content="newest", created_at=base + timedelta(minutes=20)
    )

    # Full asc list establishes the expected order.
    full = await svc.memory_list_by_filters(tenant_id=tenant, order="asc")
    full_ids = [str(m.id) for m in full]
    assert full_ids == [m1, m2, m3]

    # Page forward from the OLDEST row's cursor — must yield the two NEWER rows.
    cursor = full[0]
    page = await svc.memory_list_by_filters(
        tenant_id=tenant,
        order="asc",
        cursor_ts=cursor.created_at,
        cursor_id=cursor.id,
    )
    assert [str(m.id) for m in page] == [m2, m3]


async def test_list_by_filters_desc_cursor_unchanged():
    """``order="desc"`` (the default) still walks toward OLDER rows — the FIX 2
    branch must not regress the desc path."""
    svc = PostgresService()
    tenant = _t()
    base = datetime.now(UTC) - timedelta(hours=1)
    m1 = await _add_memory(svc, tenant, content="oldest", created_at=base)
    m2 = await _add_memory(
        svc, tenant, content="middle", created_at=base + timedelta(minutes=10)
    )
    m3 = await _add_memory(
        svc, tenant, content="newest", created_at=base + timedelta(minutes=20)
    )

    full = await svc.memory_list_by_filters(tenant_id=tenant, order="desc")
    assert [str(m.id) for m in full] == [m3, m2, m1]

    # Page forward from the NEWEST row's cursor — must yield the two OLDER rows.
    cursor = full[0]
    page = await svc.memory_list_by_filters(
        tenant_id=tenant,
        order="desc",
        cursor_ts=cursor.created_at,
        cursor_id=cursor.id,
    )
    assert [str(m.id) for m in page] == [m2, m1]
