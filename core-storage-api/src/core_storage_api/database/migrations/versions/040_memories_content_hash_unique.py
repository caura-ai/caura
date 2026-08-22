"""One live memory per ``(tenant, fleet, agent, content_hash)``.

H-04 second half (OSS #814). ``ix_memories_content_hash`` is NON-unique and
covers only ``(tenant_id, content_hash)``, so nothing in the schema has ever
enforced the dedup contract the write path advertises. #839 stopped the wedge —
two live rows sharing a hash made ``scalar_one_or_none()`` raise
``MultipleResultsFound`` → storage 500 → every subsequent write of that content
500ing forever — but it stopped it by *tolerating* duplicates (oldest wins).
This closes the hole instead of degrading around it.

MEASURED ON PROD BEFORE WRITING THIS, because a unique index over existing data
either applies or fails the deploy — there is no third outcome:

    duplicate_groups: 18   surplus_rows: 19   worst_group: 3   live_rows: 110,057

So the index would fail today, and the cleanup is 19 rows. The ``GROUP BY`` in
that query is character-for-character the key below, so the count really does
describe this index and not an approximation of it.

CLEANUP FIRST, in the normal transaction. Migrations run in the FastAPI lifespan
startup hook, so a failed one is a failed deploy (Cloud Run keeps the old
revision serving — it fails safe, but it fails). Same ordering rationale as
``038_documents_timestamps_not_null``.

The survivor is the OLDEST row per group, ordered ``(created_at, id)``. That is
not an arbitrary tie-break: it is exactly what ``memory_find_by_content_hash``
returns after #839, so the row this migration keeps is the row the dedup gate
would already have handed callers. Anything else would silently re-point live
lookups at a different row. ``id`` breaks ties because ties are the COMMON case
here rather than an edge — ``created_at`` is ``server_default=now()``, fixed for
a whole transaction, and the auto-chunk path inserts all its children in one
call, so duplicates minted that way share ``created_at`` exactly.

The surplus rows are SOFT-deleted, never hard-deleted, and stamped
``metadata.deduped_by_migration`` so all 19 stay findable afterwards:

    SELECT id FROM memories WHERE metadata ->> 'deduped_by_migration' = '040'

Soft is what makes this reviewable rather than a mass mutation — the rows keep
their content, their entity links and their children, and the predicate below
excludes them from the index by construction.

Revision ID: 040
Revises: 039
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "040"
down_revision: str | None = "039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "uq_memories_live_content_hash"

# The key, in one place: the cleanup's PARTITION BY and the index's column list
# must not drift apart, or the migration deletes rows against one definition and
# then builds a constraint over another.
_KEY = "tenant_id, COALESCE(fleet_id, ''), agent_id, content_hash"

# Matches the index predicate. ``content_hash`` is nullable and a NULL means
# "not hashed", not "hashed to nothing", so those rows are outside the contract
# entirely; ``deleted_at IS NULL`` keeps soft-deleted rows out, which is what
# lets the cleanup below resolve a conflict by soft-deleting.
_LIVE = "deleted_at IS NULL AND content_hash IS NOT NULL"

# Module-level, and public, so the test can execute the EXACT statement this
# migration runs. The cleanup is the part that mutates rows, and it had never
# once run against a real duplicate — both local databases were already clean, so
# it went green having soft-deleted nothing. A test that re-typed the SQL would
# have pinned a copy rather than this.
CLEANUP_SQL = f"""
    WITH ranked AS (
        SELECT id,
               row_number() OVER (
                   PARTITION BY {_KEY}
                   ORDER BY created_at, id
               ) AS rn
        FROM memories
        WHERE {_LIVE}
    )
    UPDATE memories m
    SET deleted_at = now(),
        status = 'deleted',
        metadata = CASE
                       WHEN jsonb_typeof(m.metadata::jsonb) = 'object'
                       THEN m.metadata::jsonb
                       ELSE '{{}}'::jsonb
                   END
                   || '{{"deduped_by_migration": "040"}}'::jsonb
    FROM ranked r
    WHERE m.id = r.id
      AND r.rn > 1
"""


def _drop_invalid(connection: sa.Connection) -> None:
    """Remove the index if an interrupted CONCURRENTLY build left it INVALID.

    Mirrors 005 / 007 / 026 / 035, and it is not theoretical: a killed
    ``CREATE INDEX CONCURRENTLY`` leaves the index in ``pg_index`` with
    ``indisvalid = false``, ``IF NOT EXISTS`` then SKIPS it ("already exists,
    skipping"), and the planner refuses to use it — 035 measured 547x slower on
    the query its index existed for.

    For a UNIQUE index the stakes are higher than a slow plan: an invalid unique
    index does not enforce uniqueness either, so without this the migration
    stamps green while the constraint it exists to add is silently absent, and
    ``memory_add`` starts trusting a guarantee it does not have.

    Reachable because CONCURRENTLY waits on concurrent transactions with no
    upper bound (035 measured 7.7 s behind a single open writer) and Cloud Run
    kills the container at the 240 s startup probe.
    """
    invalid = connection.execute(
        sa.text(
            """
            SELECT 1 FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            WHERE c.relname = :name
              AND NOT i.indisvalid
            """
        ),
        {"name": _INDEX_NAME},
    ).fetchone()
    if invalid:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")


def upgrade() -> None:
    # Row locks on the surplus rows only (19 in prod), so this stays in the
    # normal transaction and commits before the DDL below.
    #
    # ``m.metadata::jsonb`` is load-bearing, not defensive. The live column is
    # ``json`` (migration 001), while ``common/models/memory.py`` declares
    # ``JSONB`` — so the type differs between the migration-chain schema and any
    # schema built from that metadata. Without the cast, COALESCE cannot unify
    # ``json`` with a ``'{}'::jsonb`` literal and the statement fails outright
    # with ``COALESCE could not convert type jsonb to json``, which on this
    # migration path means a failed deploy. Casting the COLUMN (rather than
    # casting the result) is what makes the same statement correct against
    # either type; verified against both.
    #
    # ``jsonb_typeof(...) = 'object'`` rather than ``COALESCE(..., '{}')``,
    # because SQL NULL is not the only non-object this column can hold: JSON
    # ``null`` is a value, COALESCE passes it straight through, and
    # ``'null'::jsonb || '{...}'::jsonb`` does not fail — it WRAPS both in an
    # array. That would silently drop the marker (``->> 'key'`` on an array is
    # NULL) and replace the row's metadata with ``[null, {...}]``. Reachable:
    # SQLAlchemy's JSON type stores a Python ``None`` as JSON ``null`` unless the
    # column sets ``none_as_null``, which this one does not. The CASE covers SQL
    # NULL, JSON null, and any array/scalar the column has ever been given.
    op.execute(CLEANUP_SQL)

    # ``autocommit_block`` commits the cleanup above before the build starts,
    # which is required both ways round: CONCURRENTLY cannot run inside a
    # transaction at all, and the build must see the soft-deletes as committed
    # or it fails on the very rows just resolved.
    with op.get_context().autocommit_block():
        connection = op.get_context().connection
        if connection is None:
            raise RuntimeError("online migration requires a connection")
        _drop_invalid(connection)
        # The ``CREATE ... INDEX ... ON <table>`` prefix stays on ONE source line
        # AND spells the index name literally, because
        # ``test_no_plain_create_index_on_large_tables`` regex-scans this file for
        # it: its ``\w+`` for the name matches neither a split prefix nor an
        # interpolated ``{_INDEX_NAME}``, which is how 007 and 026 ended up
        # outside its coverage despite doing the right thing. Hence the one
        # deliberate duplication of the name in this file — being inside the
        # guard is worth more than the constant. The trailing key and predicate
        # may wrap; the regex stops at the table name.
        op.execute(
            "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_memories_live_content_hash ON memories"
            f" ({_KEY}) WHERE {_LIVE}"
        )


def downgrade() -> None:
    # Only the index is reversed. The soft-deletes are deliberately NOT undone:
    # restoring them would re-create the exact duplicate groups this migration
    # resolved, and on a re-upgrade they would be soft-deleted again — a loop
    # that mutates rows on every schema round-trip. They remain recoverable by
    # hand via the ``metadata.deduped_by_migration = '040'`` marker, which is
    # why the marker is written.
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
