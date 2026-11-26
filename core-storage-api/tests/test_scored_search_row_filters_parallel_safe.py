"""The scored search's row filters leave its tenant scan parallel-safe.

The scored search's ingredients CTE scores every row of the tenant that passes
``_scored_search_row_filters``. On a large tenant that scan is the whole cost of a
search, and Postgres can spread it over parallel workers only when every predicate
in its filter is parallel-safe. One parallel-restricted predicate, such as a
correlated subquery, makes Postgres run the whole scan in one process.

#2373's successor check was a correlated ``EXISTS`` and did exactly that. On a
tenant of ~240k memories, the same ~4.2M buffer reads ran in 1.5 s with four
workers and in about 6 s without them, enough for searches under concurrent load
to reach the reader's statement timeout.

``debug_parallel_query = on`` puts a ``Gather`` on top of any plan that is
parallel-safe and leaves the plan alone otherwise, so the ``Gather`` shows whether
the filters are parallel-safe whatever the table's size or statistics.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from common.models.memory import Memory
from core_storage_api.services.postgres_service import (
    _fts_guard_clause,
    _scored_search_row_filters,
    _successor_fts_clause,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_QUERY = "workflow rules for merge and deploy"


def _row_filter_scan(readable_tenant_ids: list[str] | None):
    """``SELECT id FROM memories WHERE <the scored search's row filters>``, built as the search builds them."""
    ts_query = func.plainto_tsquery("english", _QUERY)
    filters = _scored_search_row_filters(
        tenant_id="t-parallel",
        readable_tenant_ids=readable_tenant_ids,
        fts_guard=_fts_guard_clause(_QUERY, ts_query),
        successor_fts=_successor_fts_clause(
            _QUERY, ts_query, tenant_id="t-parallel", readable_tenant_ids=readable_tenant_ids
        ),
        fleet_ids=None,
        strict_fleet_scoping=False,
        caller_agent_id=None,
        caller_tenant_id=None,
        filter_agent_id=None,
        memory_type_filter=None,
        status_filter=None,
        history_query=False,
        valid_at=None,
    )
    names = [name for name, _ in filters]
    assert "status_superseded" in names, names
    return select(Memory.id).where(*(predicate for _, predicate in filters))


async def _plan(stmt) -> str:
    """``EXPLAIN`` of ``stmt`` with bound parameters, as the app sends it.

    Not ``literal_binds``: ``plainto_tsquery``'s ``regconfig`` argument has no
    literal renderer. The statement goes through asyncpg's own ``$n`` parameters
    instead, on the same connection as the ``SET LOCAL``s.
    """
    from core_storage_api.database.init import get_engine

    async with get_engine().connect() as conn:
        await conn.execute(text("SET LOCAL max_parallel_workers_per_gather = 2"))
        await conn.execute(text("SET LOCAL debug_parallel_query = on"))
        compiled = stmt.compile(dialect=conn.dialect, compile_kwargs={"render_postcompile": True})
        params = [compiled.params[name] for name in compiled.positiontup or []]
        raw = await conn.get_raw_connection()
        rows = await raw.driver_connection.fetch(f"EXPLAIN {compiled.string}", *params)
        return "\n".join(row[0] for row in rows)


@pytest.mark.parametrize(
    "readable_tenant_ids",
    [None, ["t-parallel", "t-parallel-2"]],
    ids=["single-tenant", "cross-tenant"],
)
async def test_row_filters_are_parallel_safe(_ensure_schema, readable_tenant_ids) -> None:
    plan = await _plan(_row_filter_scan(readable_tenant_ids))
    # The successor check is the filters' only subquery. It must be in the plan,
    # or the test proves nothing.
    assert "SubPlan" in plan, plan
    assert plan.lstrip().startswith("Gather"), (
        "a scored-search row filter is parallel-restricted, so the tenant scan "
        f"would run in one process:\n{plan}"
    )
