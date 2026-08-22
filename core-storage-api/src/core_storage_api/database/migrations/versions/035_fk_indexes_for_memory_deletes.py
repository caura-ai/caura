"""Index the FK columns that reference ``memories.id`` / ``entities.id``.

Deleting a parent row makes PostgreSQL enforce every foreign key that POINTS AT
it, and enforcement needs an index on the REFERENCING side. Three were missing
one, so each deleted row cost a full scan of the referencing table:

    memories.supersedes_id           -> memories.id   SET NULL   no index
    relations.evidence_memory_id     -> memories.id   SET NULL   no index
    memory_entity_links.entity_id    -> entities.id   CASCADE    no usable index

The last is subtle: ``memory_entity_links`` has PK ``(memory_id, entity_id)``, so
``memory_id`` is covered as the leading column but ``entity_id`` is not — a btree
cannot serve a trailing column as a prefix.

Measured on a 30,801-row ``memories`` table, deleting 4,334 rows in one statement
(``EXPLAIN ANALYZE``), before and after this migration:

    Trigger memories_supersedes_id_fkey:        15343 ms  ->  8.7 ms
    Trigger memory_entity_links_memory_id_fkey:    77 ms  -> 29.7 ms
    Trigger relations_evidence_memory_id_fkey:     17 ms  ->  7.4 ms

The supersedes_id trigger was **98.7% of the statement**, at 3.5 ms per deleted
row — one scan of the whole table each time, even though only 87 rows had a
non-NULL ``supersedes_id``. The cost is the scan, not the matches. It is linear
in table size for a fixed batch (prod's ~92k rows would be ~3x worse) and
quadratic for a fixed-FRACTION purge, where the batch grows with the table.

Not a test-only concern. ``memory_purge_soft_deleted`` (batched, driven by the
CAURA-656 retention fanout), ``purge_tenant_data`` and ``purge_fleet_data`` all
delete from these tables in bulk. The RI trigger scans the WHOLE referencing
table, not the tenant's slice, so deleting a tenant's children first does not
shrink it — every other tenant's rows are still scanned once per deleted parent.

``relations.evidence_memory_id`` is currently unmeasurable (that table is
near-empty in dev) but is included deliberately: it is written by a live
co-occurrence inference path, so it grows pairwise, and the index costs one
btree entry per insert against a projected ~5.7 s per 500-row retention batch at
prod scale.

Revision ID: 035
Revises: 034
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "035"
down_revision: str | None = "034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAMES = (
    "ix_memories_supersedes_id",
    "ix_relations_evidence_memory",
    "ix_memory_entity_links_entity_id",
)


def _drop_invalid(connection: sa.Connection) -> None:
    """Remove any index left INVALID by an interrupted CONCURRENTLY build.

    Mirrors 005 / 007 / 026, and it is not theoretical: a killed
    ``CREATE INDEX CONCURRENTLY`` leaves the index in ``pg_index`` with
    ``indisvalid = false``, ``IF NOT EXISTS`` then SKIPS it ("already exists,
    skipping"), and the planner refuses to use it — measured 547x slower than
    the valid index on the very query the FK trigger runs. Without this the
    migration would stamp green while leaving an index that is permanently
    useless and still charges write overhead on every insert.

    Reachable here because ``CONCURRENTLY`` waits for concurrent transactions
    with no upper bound (measured: 7.7 s behind one open writer), and Cloud Run
    kills the container at the 240 s startup probe.
    """
    for name in _INDEX_NAMES:
        invalid = connection.execute(
            sa.text(
                """
                SELECT 1 FROM pg_index i
                JOIN pg_class c ON c.oid = i.indexrelid
                WHERE c.relname = :name
                  AND NOT i.indisvalid
                """
            ),
            {"name": name},
        ).fetchone()
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")


def upgrade() -> None:
    with op.get_context().autocommit_block():
        connection = op.get_context().connection
        if connection is None:
            raise RuntimeError("online migration requires a connection")
        _drop_invalid(connection)
        # The ``CREATE INDEX ... ON <table>`` prefix stays on ONE source line, as
        # literal SQL rather than an f-string: ``test_no_plain_create_index_on_large_tables``
        # regex-scans this file for it, and matches neither a split prefix nor an
        # interpolated ``{name}`` — which is why 007 and 026 are silently outside
        # its coverage. A trailing WHERE may wrap; the regex stops at the table.
        #
        # PARTIAL on the two nullable columns. The RI check looks for rows whose
        # FK equals the deleted parent's id, which is never NULL, so excluding
        # NULLs keeps the index fully usable while dropping almost all of it:
        # 88 of 27,379 memories carry a ``supersedes_id``, and the index goes
        # from 232 kB to 16 kB. That is write cost too — a plain btree indexes
        # NULLs, so every memory insert paid an entry it could never match.
        # Verified rather than assumed: the RI plan is
        # ``Index Scan using ix_memories_supersedes_id`` with the partial index in
        # place, and the FK trigger over 1000 deletes measured 1.848 ms against
        # the full index's 2.145 ms.
        #
        # ``memory_entity_links.entity_id`` is NOT NULL (half of the PK), so a
        # predicate there would exclude nothing and is deliberately omitted.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_supersedes_id ON memories (supersedes_id)"
            " WHERE supersedes_id IS NOT NULL"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_relations_evidence_memory ON relations (evidence_memory_id)"
            " WHERE evidence_memory_id IS NOT NULL"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memory_entity_links_entity_id ON memory_entity_links (entity_id)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for name in reversed(_INDEX_NAMES):
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
