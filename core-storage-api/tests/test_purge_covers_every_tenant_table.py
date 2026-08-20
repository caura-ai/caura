"""Org hard-purge must reach every tenant-scoped table (OSS #819).

``purge_tenant_data`` backs ``POST /admin/org/purge-data``, documented as the hard
side of organization deletion. It deletes only what ``_PURGE_TENANT_TABLES``
lists, and that tuple never grew as later migrations added tenant-scoped tables.
None of them had an ON DELETE CASCADE path from a purged parent, so their rows
SURVIVED: the endpoint reported per-table counts and success while the tenant's
content was still queryable by ``tenant_id``.

``count_tenant_data`` — the deletion preview — iterates the same tuples, so it
under-reported by exactly the same set.

The first test here is the one that matters longest. It derives the expected set
from the LIVE SCHEMA rather than from a hand-written list, so the next migration
that adds a tenant-scoped table fails it instead of quietly reintroducing this
bug. That is how ``tenant_usage_counters`` was found: it postdates the audit that
filed this issue, so the issue's own list of six was already stale.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from core_storage_api.services.postgres_service import (
    _PURGE_FLEET_TABLES,
    _PURGE_ORG_KEYED_TABLES,
    _PURGE_TENANT_TABLES,
    _RETAINED_TENANT_TABLES,
    PostgresService,
    get_session,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _tables_with_column(column: str) -> set[str]:
    async with get_session() as session:
        rows = await session.execute(
            text(
                """
                SELECT table_name FROM information_schema.columns
                WHERE table_schema = 'public' AND column_name = :column
                """
            ),
            {"column": column},
        )
        return {r[0] for r in rows}


_TYPE_FILLERS = {
    "text": lambda: f"x-{uuid.uuid4().hex[:8]}",
    "character varying": lambda: f"x-{uuid.uuid4().hex[:8]}",
    "uuid": lambda: str(uuid.uuid4()),
    "timestamp with time zone": lambda: datetime.now(UTC),
    "timestamp without time zone": lambda: datetime.now(UTC).replace(tzinfo=None),
    "bytea": lambda: b"\x00",
    "integer": lambda: 0,
    "bigint": lambda: 0,
    "smallint": lambda: 0,
    "numeric": lambda: 0,
    "double precision": lambda: 0.0,
    "boolean": lambda: False,
    "jsonb": lambda: "{}",
    "json": lambda: "{}",
}


# Columns whose value a CHECK constraint restricts, so a random filler is
# rejected. Kept small and explicit rather than parsed out of
# ``pg_get_constraintdef``: two entries is not worth a constraint parser, and if a
# third appears the IntegrityError names the constraint, which points straight
# here.
_CONSTRAINED_VALUES = {
    ("session_traces", "outcome_label"): "unknown",
    ("capability_usage", "transport"): "rest",
}


async def _insert_minimal_row(session, table: str, *, tenant: str, fleet: str | None = None):
    """Insert one row into ``table`` with only its required columns populated.

    The required set is read from ``information_schema`` rather than hard-coded,
    for the same reason the coverage test derives its table list that way: a
    migration that adds a NOT NULL column should not silently turn this into a
    test of nothing (or a confusing IntegrityError). If a type shows up that has
    no filler, the assertion below says which one rather than failing obscurely.
    """
    rows = await session.execute(
        text(
            """
            SELECT column_name, data_type FROM information_schema.columns
            WHERE table_name = :table
              AND is_nullable = 'NO'
              AND column_default IS NULL
            ORDER BY ordinal_position
            """
        ),
        {"table": table},
    )
    values: dict[str, object] = {}
    for column, data_type in rows:
        constrained = _CONSTRAINED_VALUES.get((table, column))
        if constrained is not None:
            values[column] = constrained
            continue
        filler = _TYPE_FILLERS.get(data_type)
        assert filler is not None, f"{table}.{column}: no filler for type {data_type!r}"
        values[column] = filler()
    values["tenant_id"] = tenant
    if fleet is not None:
        values["fleet_id"] = fleet

    columns = ", ".join(values)
    placeholders = ", ".join(f":{c}" for c in values)
    await session.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"), values)


async def _cascades_from(table: str, parents: set[str]) -> bool:
    """True if ``table`` is removed by an ON DELETE CASCADE from a purged parent."""
    async with get_session() as session:
        rows = await session.execute(
            text(
                """
                SELECT ccu.table_name, rc.delete_rule
                FROM information_schema.table_constraints tc
                JOIN information_schema.constraint_column_usage ccu
                  ON tc.constraint_name = ccu.constraint_name
                JOIN information_schema.referential_constraints rc
                  ON tc.constraint_name = rc.constraint_name
                WHERE tc.table_name = :table AND tc.constraint_type = 'FOREIGN KEY'
                """
            ),
            {"table": table},
        )
        return any(parent in parents and rule == "CASCADE" for parent, rule in rows)


async def test_every_tenant_scoped_table_is_purged_or_deliberately_retained(
    _ensure_schema,
) -> None:
    """The guard that outlives this fix.

    Derived from the live schema, so a migration that adds a ``tenant_id`` table
    without deciding its purge fate FAILS HERE rather than silently leaving
    tenant data behind an endpoint that promises to destroy it.

    Three acceptable fates, and a table must have exactly one:
      * listed in ``_PURGE_TENANT_TABLES`` — deleted directly;
      * reachable by ON DELETE CASCADE from a purged parent;
      * listed in ``_RETAINED_TENANT_TABLES`` — a recorded decision.
    """
    purged = set(_PURGE_TENANT_TABLES) | set(_PURGE_ORG_KEYED_TABLES)
    retained = set(_RETAINED_TENANT_TABLES)

    unaccounted = []
    for table in sorted(await _tables_with_column("tenant_id")):
        if table in purged or table in retained:
            continue
        if await _cascades_from(table, purged):
            continue
        unaccounted.append(table)

    assert not unaccounted, (
        "these tenant-scoped tables survive an org hard-delete: "
        f"{unaccounted}. Add each to _PURGE_TENANT_TABLES, or to "
        "_RETAINED_TENANT_TABLES with the reason it is kept."
    )


async def test_the_retained_list_is_not_a_way_to_hide_a_purge_gap(_ensure_schema) -> None:
    """``_RETAINED_TENANT_TABLES`` exists to record decisions, so it must not
    overlap the purge list (which would make one of the two a lie) and must not
    name tables that do not exist (which would make it stale cover)."""
    purged = set(_PURGE_TENANT_TABLES) | set(_PURGE_ORG_KEYED_TABLES)
    retained = set(_RETAINED_TENANT_TABLES)

    assert not (purged & retained), f"listed as both purged and retained: {purged & retained}"

    real = await _tables_with_column("tenant_id") | await _tables_with_column("org_id")
    assert retained <= real, f"retained list names tables that do not exist: {retained - real}"


async def test_the_fleet_purge_covers_every_fleet_scoped_purged_table(_ensure_schema) -> None:
    """The sibling gap. A table that is both tenant-purged AND carries its own
    ``fleet_id`` should be reachable by the fleet-scoped purge too — otherwise a
    single fleet's teardown leaves its rows behind in a shared tenant.

    ``fleet_commands`` is the documented exception: it has no ``fleet_id`` of its
    own and ``purge_fleet_data`` deletes it explicitly by node id.
    """
    fleet_scoped = await _tables_with_column("fleet_id")
    expected = {t for t in _PURGE_TENANT_TABLES if t in fleet_scoped}
    missing = expected - set(_PURGE_FLEET_TABLES)

    assert not missing, (
        f"these carry fleet_id and are tenant-purged, but the fleet purge misses them: {sorted(missing)}"
    )


async def test_purge_actually_deletes_from_the_newly_covered_tables(_ensure_schema) -> None:
    """End-to-end, not just list membership: a row in each newly covered table is
    gone after the purge, and the returned counts say so."""
    suffix = uuid.uuid4().hex[:8]
    tenant = f"t-purge819-{suffix}"
    new_tables = (
        "recall_event",
        "session_traces",
        "agent_activity_digests",
        "forge_rejected_fingerprints",
        "capability_usage",
        "tenant_usage_counters",
        "audit_chain_head",
    )

    async with get_session() as session:
        for table in new_tables:
            await _insert_minimal_row(session, table, tenant=tenant)

    counts = await PostgresService().count_tenant_data(tenant)
    for table in new_tables:
        assert counts.get(table) == 1, f"the deletion preview under-reports {table}: {counts.get(table)!r}"

    deleted = await PostgresService().purge_tenant_data(tenant)
    for table in new_tables:
        assert deleted.get(table) == 1, f"purge did not report deleting from {table}"

    async with get_session() as session:
        for table in new_tables:
            remaining = await session.execute(
                text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"), {"t": tenant}
            )
            assert remaining.scalar() == 0, f"{table} still holds the tenant's rows"


async def test_the_fleet_purge_leaves_a_sibling_fleet_alone(_ensure_schema) -> None:
    """The fleet purge is used to clean one fleet out of a SHARED tenant, so
    over-reach is the failure that matters: it must not take a sibling fleet's
    rows with it."""
    suffix = uuid.uuid4().hex[:8]
    tenant = f"t-fleet819-{suffix}"
    doomed, kept = f"fleet-doomed-{suffix}", f"fleet-kept-{suffix}"
    fleet_tables = ("session_traces", "agent_activity_digests", "forge_rejected_fingerprints")

    async with get_session() as session:
        for table in fleet_tables:
            for fleet in (doomed, kept):
                await _insert_minimal_row(session, table, tenant=tenant, fleet=fleet)

    await PostgresService().purge_fleet_data(tenant, doomed)

    async with get_session() as session:
        for table in fleet_tables:
            gone = await session.execute(
                text(f"SELECT count(*) FROM {table} WHERE fleet_id = :f"), {"f": doomed}
            )
            assert gone.scalar() == 0, f"{table} kept the purged fleet's rows"
            survived = await session.execute(
                text(f"SELECT count(*) FROM {table} WHERE fleet_id = :f"), {"f": kept}
            )
            assert survived.scalar() == 1, f"{table} lost a SIBLING fleet's rows"
