"""Memory aggregation stats — shared by REST `/memories/stats` and MCP `memclaw_stats`.

Pure DB-bound aggregation: count totals plus group-by breakdowns by type,
agent, and status. Visibility scoping mirrors `memory_repository.list_by_filters`
(memory_repository.py:125-137) so `/memories/stats.total` matches
`/memories.length` exactly — no count-vs-list mismatch.

Callers handle their own auth + transient-DB fallback policy. This module
is import-safe (no FastAPI imports).
"""

from __future__ import annotations

from sqlalchemy import and_, or_, select, text
from sqlalchemy.dialects import postgresql
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
    readable_tenant_ids: list[str] | None = None,
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
    deleted``).

    **Cross-tenant widening:** when ``readable_tenant_ids`` is a non-empty
    list, the scope predicate expands from ``tenant_id = $1`` to
    ``tenant_id = ANY($1)`` and the result includes a ``by_tenant`` dict
    (tenant_id → count) so the caller can see per-tenant breakdown along
    with the aggregate. Mirrors ``list_by_filters`` widening (#154).
    """
    # Scope filters apply equally to live and soft-deleted rows; only the
    # ``deleted_at`` predicate flips between the two counts.
    scope_filters = []
    if readable_tenant_ids:
        scope_filters.append(Memory.tenant_id.in_(readable_tenant_ids))
    elif tenant_id:
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

    # ── Single-pass aggregation via GROUPING SETS ──
    #
    # The prior implementation issued 4-6 separate ``await db.execute(...)``
    # calls against the same filter predicate — total, by_type, by_agent,
    # by_status, plus by_tenant (cross-tenant only) and deleted (when
    # ``include_deleted``). Wet-tested at 514ms median (range 446-592ms)
    # for a populated tenant on staging. Audit finding #26.
    #
    # GROUPING SETS lets Postgres do one scan and emit aggregate rows
    # for every grouping in a single result. Each output row carries a
    # ``grouping_id`` we use to dispatch back into the per-axis dicts.
    # We always include the empty grouping set ``()`` — that row is the
    # overall total. The ``by_tenant`` grouping is conditional so the
    # query stays cheap on the common single-tenant path.
    #
    # When ``include_deleted`` is set, the second ``WHERE deleted_at IS
    # NOT NULL`` query is fused via a single CTE that selects both live
    # and tombstoned rows tagged with a flag, then aggregates each side
    # with ``FILTER (WHERE …)`` — still one round-trip.
    include_by_tenant = bool(readable_tenant_ids and len(readable_tenant_ids) > 1)

    grouping_sets = ["()", "(memory_type)", "(agent_id)", "(status)"]
    if include_by_tenant:
        grouping_sets.append("(tenant_id)")
    grouping_sets_sql = ", ".join(grouping_sets)

    # GROUPING(col) is only defined when ``col`` participates in some
    # grouping set in the same query level. Build the bucket-dispatch
    # CASE arms only for axes that actually appear in
    # ``grouping_sets``; otherwise Postgres raises
    # ``arguments to GROUPING must be grouping expressions``.
    grouping_total_cols = ["memory_type", "agent_id", "status"]
    if include_by_tenant:
        grouping_total_cols.append("tenant_id")
    grouping_total_arg = ", ".join(grouping_total_cols)
    grouping_total_value = (1 << len(grouping_total_cols)) - 1  # all bits set
    bucket_when = [
        f"WHEN GROUPING({grouping_total_arg}) = {grouping_total_value} THEN 'total'",
        "WHEN GROUPING(memory_type) = 0 THEN 'by_type'",
        "WHEN GROUPING(agent_id) = 0 THEN 'by_agent'",
        "WHEN GROUPING(status) = 0 THEN 'by_status'",
    ]
    if include_by_tenant:
        bucket_when.append("WHEN GROUPING(tenant_id) = 0 THEN 'by_tenant'")
    bucket_when_sql = "\n                ".join(bucket_when)

    # Compile the SQLAlchemy filter expressions to a SQL fragment with
    # inline-bound literals so the dynamic visibility / scoping rules
    # don't need to be re-expressed in raw SQL. ``literal_binds=True``
    # is safe here — every value in ``scope_filters`` originates from
    # typed FastAPI Query params or from the auth context (a list of
    # typed strs), never from a free-form request body. There is no
    # user-controlled string concatenated into the SQL output.
    def _predicate_sql(filter_list) -> str:
        compiled = str(
            select(Memory.id)
            .where(*filter_list)
            .compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        idx = compiled.upper().find("WHERE ")
        return compiled[idx + len("WHERE ") :] if idx >= 0 else "TRUE"

    if include_deleted:
        # Unify live + tombstoned rows in one CTE; an ``alive`` flag
        # splits them and ``FILTER (WHERE alive)`` / ``WHERE NOT alive``
        # produce both counts in a single scan. Mirrors the prior
        # 2-query behaviour exactly.
        all_predicate = _predicate_sql(scope_filters)
    # Only project columns that appear in some grouping set. Selecting
    # ``tenant_id`` without a corresponding grouping triggers "column
    # must appear in the GROUP BY clause or be used in an aggregate
    # function". The common single-tenant path skips it entirely.
    select_cols = "memory_type, agent_id, status"
    if include_by_tenant:
        select_cols = f"{select_cols}, tenant_id"

    if include_deleted:
        sql = f"""
        WITH base AS (
            SELECT memory_type, agent_id, status, tenant_id,
                   (deleted_at IS NULL) AS alive
            FROM memories
            WHERE {all_predicate}
        )
        SELECT
            CASE
                {bucket_when_sql}
            END                                              AS bucket,
            {select_cols},
            COUNT(*) FILTER (WHERE alive)                    AS live_cnt,
            COUNT(*) FILTER (WHERE NOT alive)                AS deleted_cnt
        FROM base
        GROUP BY GROUPING SETS ({grouping_sets_sql})
        """
    else:
        predicate = _predicate_sql(filters)
        sql = f"""
        SELECT
            CASE
                {bucket_when_sql}
            END                                              AS bucket,
            {select_cols},
            COUNT(*)                                         AS live_cnt,
            0                                                AS deleted_cnt
        FROM memories
        WHERE {predicate}
        GROUP BY GROUPING SETS ({grouping_sets_sql})
        """

    rows = (await db.execute(text(sql))).all()

    total = 0
    by_type: dict = {}
    by_agent: dict = {}
    by_status: dict = {}
    by_tenant: dict = {}
    deleted = 0

    # Column layout in the result row:
    #   0: bucket
    #   1: memory_type
    #   2: agent_id
    #   3: status
    #   4: tenant_id (only when include_by_tenant)
    #   live_idx, deleted_idx: shift +1 when tenant_id is present
    live_idx = 5 if include_by_tenant else 4
    deleted_idx = live_idx + 1

    for row in rows:
        bucket = row[0]
        live = int(row[live_idx] or 0)
        dead = int(row[deleted_idx] or 0)
        if bucket == "total":
            total = live
            deleted = dead
        elif bucket == "by_type":
            by_type[row[1]] = live
        elif bucket == "by_agent":
            by_agent[row[2]] = live
        elif bucket == "by_status":
            by_status[row[3]] = live
        elif bucket == "by_tenant":
            by_tenant[row[4]] = live

    result = {
        "total": total,
        "by_type": by_type,
        "by_agent": by_agent,
        "by_status": by_status,
    }
    if include_by_tenant:
        result["by_tenant"] = by_tenant
    if include_deleted:
        result["deleted"] = deleted
        result["total_including_deleted"] = total + deleted
    return result
