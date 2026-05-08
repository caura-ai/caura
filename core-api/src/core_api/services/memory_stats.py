"""Memory aggregation stats — shared by REST `/memories/stats` and MCP `memclaw_stats`.

Pure DB-bound aggregation: count totals plus group-by breakdowns by type,
agent, and status. Visibility scoping mirrors `memory_repository.list_by_filters`
(memory_repository.py:125-137) so `/memories/stats.total` matches
`/memories.length` exactly — no count-vs-list mismatch.

Callers handle their own auth + transient-DB fallback policy. This module
is import-safe (no FastAPI imports).
"""

from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.memory import Memory
from core_api.constants import (
    MEMORY_VISIBILITY_SCOPE_AGENT,
    MEMORY_VISIBILITY_SCOPE_ORG,
    MEMORY_VISIBILITY_SCOPE_TEAM,
)


async def compute_memory_stats(
    db: AsyncSession,
    *,
    tenant_id: str | None,
    fleet_id: str | None = None,
    agent_id: str | None = None,
    memory_type: str | None = None,
    status: str | None = None,
    include_deleted: bool = False,
) -> dict:
    """Return ``{total, by_type, by_agent, by_status}`` for the given filters.

    When ``agent_id`` is provided it doubles as the visibility identity AND
    the author filter (matches the REST handler and ``list_by_filters``).
    When omitted, ``scope_agent`` rows are excluded so totals never include
    memories that ``/memories`` would never return for the same caller.

    ``total`` and the breakdowns always exclude soft-deleted rows
    (``deleted_at IS NULL``) so they stay aligned with what ``/memories``
    would return. When ``include_deleted=True`` the result additionally
    carries ``deleted`` (count of soft-deleted rows matching the same
    scoping filters) and ``total_including_deleted`` (= ``total +
    deleted``). The breakdown dicts deliberately stay non-deleted only —
    they should match ``total``, not ``total_including_deleted``.
    """
    # Scope filters apply equally to live and soft-deleted rows; only the
    # ``deleted_at`` predicate flips between the two counts.
    scope_filters = []
    if tenant_id:
        scope_filters.append(Memory.tenant_id == tenant_id)
    if fleet_id:
        scope_filters.append(Memory.fleet_id == fleet_id)
    if agent_id:
        scope_filters.append(Memory.agent_id == agent_id)
        scope_filters.append(
            or_(
                Memory.visibility == MEMORY_VISIBILITY_SCOPE_ORG,
                Memory.visibility == MEMORY_VISIBILITY_SCOPE_TEAM,
                and_(
                    Memory.visibility == MEMORY_VISIBILITY_SCOPE_AGENT,
                    Memory.agent_id == agent_id,
                ),
            )
        )
    else:
        scope_filters.append(Memory.visibility != MEMORY_VISIBILITY_SCOPE_AGENT)
    if memory_type:
        scope_filters.append(Memory.memory_type == memory_type)
    if status:
        scope_filters.append(Memory.status == status)

    filters = [Memory.deleted_at.is_(None), *scope_filters]
    base = select(Memory).where(*filters)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar()
    by_type = dict(
        (
            await db.execute(
                select(Memory.memory_type, func.count()).where(*filters).group_by(Memory.memory_type)
            )
        ).all()
    )
    by_agent = dict(
        (
            await db.execute(select(Memory.agent_id, func.count()).where(*filters).group_by(Memory.agent_id))
        ).all()
    )
    by_status = dict(
        (await db.execute(select(Memory.status, func.count()).where(*filters).group_by(Memory.status))).all()
    )
    result = {
        "total": total,
        "by_type": by_type,
        "by_agent": by_agent,
        "by_status": by_status,
    }
    if include_deleted:
        deleted_filters = [Memory.deleted_at.is_not(None), *scope_filters]
        deleted = (
            await db.execute(
                select(func.count()).select_from(select(Memory).where(*deleted_filters).subquery())
            )
        ).scalar()
        result["deleted"] = deleted
        result["total_including_deleted"] = total + deleted
    return result
