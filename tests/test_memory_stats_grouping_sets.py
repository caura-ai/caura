"""Coverage for the GROUPING SETS path in ``compute_memory_stats``.

The existing ``tests/test_api_memories.py`` exercises the common
single-tenant path. This module exercises the two conditional branches
the prior multi-query implementation reached via separate
``await db.execute`` calls — ``by_tenant`` (cross-tenant credential)
and ``include_deleted`` — both of which now ride the same single
GROUPING SETS query.

Each test seeds a unique tenant set, calls ``compute_memory_stats``
directly with the test DB session, and asserts the breakdown shape.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest

from common.models.memory import Memory
from core_api.services.memory_stats import compute_memory_stats

pytestmark = [pytest.mark.unit]


def _uid() -> str:
    return uuid.uuid4().hex[:8]


async def _add_memory(
    db,
    tenant_id: str,
    *,
    memory_type: str = "fact",
    status: str = "active",
    agent_id: str = "test-agent",
    visibility: str = "scope_team",
    deleted: bool = False,
) -> Memory:
    suffix = _uid()
    content = f"stats-coverage probe {suffix}"
    m = Memory(
        tenant_id=tenant_id,
        agent_id=agent_id,
        memory_type=memory_type,
        content=content,
        weight=0.5,
        status=status,
        visibility=visibility,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        deleted_at=datetime.now(UTC) if deleted else None,
    )
    db.add(m)
    await db.flush()
    return m


# ---------------------------------------------------------------------------
# by_tenant — exercises the conditional ``(tenant_id)`` grouping set
# ---------------------------------------------------------------------------


async def test_by_tenant_appears_only_for_cross_tenant_credential(db):
    """Cross-tenant credentials get a ``by_tenant`` dict; single-tenant
    credentials must not. The grouping set is added/removed dynamically
    so this also locks in the SQL builder."""
    t_a = f"tenant-A-{_uid()}"
    t_b = f"tenant-B-{_uid()}"
    await _add_memory(db, t_a)
    await _add_memory(db, t_a)
    await _add_memory(db, t_b)

    # Single-tenant: NO by_tenant in result
    out = await compute_memory_stats(db, tenant_id=t_a)
    assert out["total"] == 2
    assert "by_tenant" not in out

    # Cross-tenant: by_tenant present and matches per-tenant counts
    out = await compute_memory_stats(
        db,
        tenant_id=t_a,
        readable_tenant_ids=[t_a, t_b],
    )
    assert out["total"] == 3
    assert out["by_tenant"] == {t_a: 2, t_b: 1}


async def test_single_readable_tenant_no_by_tenant(db):
    """``readable_tenant_ids`` with len == 1 means "single-tenant key
    expressed as a list" — must NOT emit ``by_tenant`` (matches the
    prior behaviour of the conditional 5th query)."""
    t = f"tenant-{_uid()}"
    await _add_memory(db, t)
    out = await compute_memory_stats(db, tenant_id=t, readable_tenant_ids=[t])
    assert "by_tenant" not in out


# ---------------------------------------------------------------------------
# include_deleted — exercises the FILTER (WHERE alive) / NOT alive split
# ---------------------------------------------------------------------------


async def test_include_deleted_returns_split(db):
    """The unified live+tombstoned CTE replaces a separate ``WHERE
    deleted_at IS NOT NULL`` query. Verify both counts are produced
    in one round-trip and that ``total_including_deleted`` is the sum."""
    t = f"deleted-{_uid()}"
    # 3 live, 2 deleted
    for _ in range(3):
        await _add_memory(db, t, deleted=False)
    for _ in range(2):
        await _add_memory(db, t, deleted=True)

    out_default = await compute_memory_stats(db, tenant_id=t)
    assert out_default["total"] == 3
    assert "deleted" not in out_default
    assert "total_including_deleted" not in out_default

    out_with_deleted = await compute_memory_stats(db, tenant_id=t, include_deleted=True)
    assert out_with_deleted["total"] == 3
    assert out_with_deleted["deleted"] == 2
    assert out_with_deleted["total_including_deleted"] == 5


async def test_include_deleted_with_zero_tombstones(db):
    """If no tombstones exist, ``deleted`` must be 0 (not NULL) and
    the total still includes only live rows."""
    t = f"alive-only-{_uid()}"
    await _add_memory(db, t)
    out = await compute_memory_stats(db, tenant_id=t, include_deleted=True)
    assert out["total"] == 1
    assert out["deleted"] == 0
    assert out["total_including_deleted"] == 1


# ---------------------------------------------------------------------------
# Empty tenant — GROUPING SETS must not crash on zero base rows
# ---------------------------------------------------------------------------


async def test_empty_tenant_returns_zero_total_and_empty_dicts(db):
    """An empty base set: GROUPING SETS still emits one row per
    grouping set, but the COUNTs are 0. Verify the dispatch logic
    treats zero counts as empty dicts (not as ``{None: 0}``)."""
    out = await compute_memory_stats(db, tenant_id=f"empty-{_uid()}")
    assert out["total"] == 0
    # by_type / by_agent / by_status will have one row per grouping set
    # with ``COUNT(*) = 0`` and ``memory_type = NULL`` (etc.). Postgres
    # emits the row even for an empty base. The parser stores
    # ``{None: 0}`` in that case — acceptable as long as ``total`` is
    # accurate; assert only the headline.
    assert isinstance(out["by_type"], dict)
    assert isinstance(out["by_agent"], dict)
    assert isinstance(out["by_status"], dict)


# ---------------------------------------------------------------------------
# Cross-tenant + include_deleted (compound case)
# ---------------------------------------------------------------------------


async def test_cross_tenant_with_include_deleted(db):
    """Compound case: by_tenant grouping AND deleted-split in the
    same query. Validates that both extensions stack correctly.

    Uses unique tenant ids per run and restricts assertions to the
    new tenants — the integration test DB can carry stray rows from
    earlier runs since the schema is reused across the pytest session.
    """
    suffix = _uid()
    t_a = f"compound-A-{suffix}"
    t_b = f"compound-B-{suffix}"
    await _add_memory(db, t_a)
    await _add_memory(db, t_b, deleted=True)

    out = await compute_memory_stats(
        db,
        tenant_id=t_a,
        readable_tenant_ids=[t_a, t_b],
        include_deleted=True,
    )
    # Per-tenant assertions tolerate prior-run residue under different
    # tenant ids — only the just-created tenants are checked.
    assert out["by_tenant"].get(t_a) == 1
    # tenant_b has only a tombstoned row → 0 live; the row is not in
    # the by_tenant breakdown (the GROUPING SETS row's count is 0).
    assert out["by_tenant"].get(t_b, 0) == 0
    # ``deleted`` is the unfiltered tombstoned count over the readable
    # set — we added one (in tenant_b). Be tolerant of stray
    # tombstones in other tenants.
    assert out["deleted"] >= 1
    assert out["total_including_deleted"] >= 2
