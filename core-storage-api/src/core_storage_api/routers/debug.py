"""Diagnostic endpoints for live DB inspection.

Off unless ``CORE_STORAGE_DEBUG_ENDPOINTS`` is set: a disabled route answers
404. When on, callers still need the storage shared secret, like every route
here. Cloud Run IAM adds a layer in SaaS only; compose and on-prem have
nothing else in front of it (L-75). Not routed through the gateway. The
endpoints here snapshot DB-side state (``pg_locks``, ``pg_stat_activity``,
``pg_blocking_pids``) for triaging contention storms; they're cheap
enough to poll at 1Hz during a controlled loadtest, but expensive
enough to NOT wire into a routine dashboard.

CAURA-686 motivation: instance-level Cloud Monitoring wait-event
metrics (``alloydb.googleapis.com/instance/postgresql/wait_time``) tell
you THAT row-level locks are accumulating but not WHICH query holds
them. ``pg_stat_activity`` joined with ``pg_locks`` and
``pg_blocking_pids`` is the canonical Postgres surface for that.
AlloyDB Insights' per-query lock_time aggregation does NOT capture
``Lock/transactionid`` / ``Lock/tuple`` — only LWLocks — so this
endpoint is the only way to attribute row-lock waits to specific
queries on AlloyDB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core_storage_api.config import settings
from core_storage_api.database.init import get_session


def _require_debug_endpoints() -> None:
    """404 unless the operator turned the debug endpoints on (L-75)."""
    if not settings.core_storage_debug_endpoints:
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(tags=["Debug"], dependencies=[Depends(_require_debug_endpoints)])


# Direct privilege check: ``pg_read_all_stats`` (PG 10+) exposes
# other backends' query / wait_event / usename; ``pg_monitor`` is a
# bundle that includes ``pg_read_all_stats``. Either grants the
# visibility this endpoint needs. Asking the catalog directly is
# cheaper and unambiguous compared to the older heuristic of
# scanning the returned rows for non-NULL ``query``/``usename``
# (which couldn't distinguish "no contention" from "no privilege").
_PG_CHECK_STATS_SQL = text(
    "SELECT pg_has_role(current_user, 'pg_read_all_stats', 'USAGE') "
    "   OR pg_has_role(current_user, 'pg_monitor', 'USAGE') AS has_priv"
)


# Combines pg_stat_activity (backend state, current query, wait shape)
# with the result of pg_blocking_pids() so each waiting backend lists
# the pids that hold the locks it's queueing on. Filtered to only
# rows that are either waiting OR holding a lock for a transaction
# that has waiters — keeps the response small under steady state
# and meaningful under a storm.
#
# Only sessions of this service's own database role (L-75): the database
# can be shared with other services (platform-storage-api's ``enterprise.*``
# sessions, an operator's psql), and their statements are not this
# endpoint's to show. A ``datname`` filter would not exclude them. Blocker
# pid lists can still name another role's pid, never its statement.
#
# CTE structure: ``backend_blockers`` calls ``pg_blocking_pids`` exactly
# once per backend (it's not cheap); ``blocker_set`` collapses the union
# of blocker pids so the final filter can do a single set lookup.
# Replaces an earlier version that called ``pg_blocking_pids`` up to
# five times per backend (once for ``WHERE``, once for the SELECT
# projection, once for ``array_length`` in WHERE + SELECT, and once
# inside the IN-list subquery).
_PG_LOCKS_SNAPSHOT_SQL = text("""
WITH backend_blockers AS (
    SELECT
        pid,
        pg_blocking_pids(pid) AS blocked_by_pids
    FROM pg_stat_activity
    WHERE pid <> pg_backend_pid()
      AND backend_type = 'client backend'
      AND usename = current_user
),
blocker_set AS (
    SELECT DISTINCT unnest(blocked_by_pids) AS pid
    FROM backend_blockers
    WHERE cardinality(blocked_by_pids) > 0
)
SELECT
    a.pid,
    a.datname,
    a.usename,
    a.application_name,
    a.state,
    a.wait_event_type,
    a.wait_event,
    EXTRACT(EPOCH FROM (now() - a.xact_start))::float  AS xact_age_sec,
    EXTRACT(EPOCH FROM (now() - a.query_start))::float AS query_age_sec,
    LEFT(a.query, 240)                                  AS query,
    bb.blocked_by_pids,
    cardinality(bb.blocked_by_pids)                     AS blocked_by_n
FROM pg_stat_activity a
JOIN backend_blockers bb ON bb.pid = a.pid
WHERE (
    a.wait_event_type IS NOT NULL
    OR cardinality(bb.blocked_by_pids) > 0
    OR a.pid IN (SELECT pid FROM blocker_set)
)
ORDER BY a.xact_start NULLS LAST, a.pid
""")


@router.get("/_debug/pg_locks")
async def pg_locks_snapshot(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return waiting backends + their blocker pids.

    Response shape::

        {
          "captured_at": "2026-05-25T15:42:13.456Z",
          "pg_read_all_stats": true,
          "rows": [
            {
              "pid": 12345,
              "wait_event_type": "Lock",
              "wait_event": "transactionid",
              "xact_age_sec": 4.231,
              "query": "INSERT INTO memories ...",
              "blocked_by_pids": [12346, 12347],
              "blocked_by_n": 2,
              ...
            },
            ...
          ]
        }

    A snapshot caller (the loadtest harness or an operator polling
    during a contrived storm) reads ``wait_event`` + ``query`` to
    attribute the lock to a specific statement, then chases
    ``blocked_by_pids`` to find the blocker's query.

    Rows are this service's own sessions only (L-75), which the role
    can always see in full, so a lock held by another role shows up
    as a pid in ``blocked_by_pids`` without its statement.
    ``pg_read_all_stats`` in the response is a direct privilege check
    via ``pg_has_role(current_user, ...)``, always ``True`` or
    ``False``; it is kept for callers that read it, and now only
    says whether chasing such a pid by hand would show its query.
    """
    priv_row = (await session.execute(_PG_CHECK_STATS_SQL)).one()
    has_visibility = priv_row.has_priv
    result = await session.execute(_PG_LOCKS_SNAPSHOT_SQL)
    rows = [dict(row._mapping) for row in result.all()]
    return {
        "captured_at": datetime.now(UTC).isoformat(),
        "pg_read_all_stats": has_visibility,
        "rows": rows,
    }
