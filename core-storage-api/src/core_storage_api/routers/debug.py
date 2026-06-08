"""Diagnostic endpoints for live DB inspection.

Internal-only surface — exposed on the storage-api private VPC IP and
gated by Cloud Run IAM. Not routed through the gateway. The endpoints
here snapshot DB-side state (``pg_locks``, ``pg_stat_activity``,
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

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core_storage_api.database.init import get_session

router = APIRouter(tags=["Debug"])


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

    Requires the app DB role to have ``pg_read_all_stats``; without
    it, ``query`` / ``usename`` / ``wait_event`` come back NULL for
    other users' backends and the snapshot is effectively blind to
    the actual lock holder. ``pg_read_all_stats`` in the response is
    a direct privilege check via ``pg_has_role(current_user, ...)``
    and is always ``True`` or ``False`` — never ``null``. ``true``
    means the role has ``pg_read_all_stats`` (or ``pg_monitor``,
    which includes it) and the snapshot rows carry useful query
    text; ``false`` means the role is missing both and the rows
    will be blind to other users' backends regardless of whether
    contention is actually present.
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
