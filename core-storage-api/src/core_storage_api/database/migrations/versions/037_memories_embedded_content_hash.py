"""Embedding provenance: ``memories.embedded_content_hash``.

``memories`` carries ``embedding`` and ``content_hash``, but nothing recording
WHICH content a given vector was computed from. A vector left over from earlier
content is therefore byte-identical to a correct one: ``embedding IS NOT NULL``
says only that something was embedded, never what. That made mis-embedded rows
undetectable — the NULL-embedding sweep cannot see them (the column is
non-NULL), and no query could distinguish them.

This column records the ``content_hash`` of the text the vector was actually
computed from, making staleness expressible:

    embedding IS NOT NULL
    AND embedded_content_hash IS NOT NULL          -- provenance known
    AND embedded_content_hash IS DISTINCT FROM content_hash

The ``IS NOT NULL`` term is load-bearing and easy to drop by accident. NULL
means "provenance unknown", and ``NULL IS DISTINCT FROM <hash>`` evaluates to
TRUE — so omitting it silently reclassifies every pre-migration row as stale,
which is precisely the conclusion this migration exists to avoid.

``IS DISTINCT FROM`` rather than ``<>`` for the comparison itself: ``<>``
against a NULL yields NULL, so a row would drop out of a ``WHERE`` clause
rather than being counted either way. Both halves are needed — one to keep
unknown OUT of stale, the other to stop rows vanishing.

NO BACKFILL, deliberately. Existing rows get NULL, which means "provenance
unknown" — NOT "stale". The two must stay distinct: marking every pre-migration
row stale would report the whole historical corpus as damaged, and marking it
fresh would assert a correctness we cannot know. Rows acquire provenance as
they are written or re-embedded, so the unknown bucket drains on its own.

The partial index carries the ``IS NOT NULL`` term. Without it the index would
cover every unknown-provenance row — on day one, the entire pre-migration table
— making a "partial" index a full-table one and inverting the reason for making
it partial at all.

It does NOT carry the detector's ``status IN LIVE_MEMORY_STATUSES`` filter, so
it is BROADER than the query it serves. That is deliberate, not an oversight.
``LIVE_MEMORY_STATUSES`` is a Python tuple in ``common/constants.py``; copying
its members into an immutable migration would put the same rule in two places
with nothing checking they agree — the exact failure this column exists to
expose, and one this PR hit twice already (an index predicate broader than the
detector, then an ORM comparison narrower than it). A literal here would rot
silently the first time that tuple changes.

The cost is bounded. The index is already restricted to rows whose vector
disagrees with their content, which in a healthy corpus is near zero whatever
the status, so the status term adds little further selectivity. Measured on a
deliberately pathological seed — every row stale, statuses spread evenly — the
index covered 3002 rows against the 1002 the query wanted; on a realistic
corpus both are small. Postgres still uses it for the narrower query, applying
``status`` as a filter on top (verified with EXPLAIN), so this is a size
question and not a correctness one.

Revision ID: 037
Revises: 036
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "037"
down_revision: str | None = "036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Metadata-only on PG11+: a nullable ADD COLUMN with no default rewrites
    # nothing, so this is safe in-transaction on a large table.
    op.add_column(
        "memories",
        sa.Column("embedded_content_hash", sa.Text(), nullable=True),
    )
    # CONCURRENTLY, in an autocommit block — ``memories`` is one of the known
    # large tables. A plain in-transaction CREATE INDEX takes an AccessExclusive
    # lock that blocks writes AND holds the migration advisory lock for the whole
    # build; that is what crashed 6 storage-writer boots on 2026-06-16 (migration
    # 025 indexed ``audit_log`` without it).
    #
    # ``op.execute`` with the ``CREATE INDEX ... ON <table>`` prefix on ONE
    # source line, as literal SQL: ``test_no_plain_create_index_on_large_tables``
    # regex-scans for exactly that shape. The first version of this migration
    # used ``op.create_index(...)``, which the guard does not inspect at all —
    # it passed while carrying precisely the defect the guard exists to prevent.
    with op.get_context().autocommit_block():
        # A CONCURRENTLY build that fails leaves an INVALID index behind, and a
        # retry would then trip over the existing name. Dropping first makes the
        # migration re-runnable after a failed attempt.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_stale_embedding")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_stale_embedding ON memories (tenant_id) "
            "WHERE embedding IS NOT NULL "
            "AND embedded_content_hash IS NOT NULL "
            "AND embedded_content_hash IS DISTINCT FROM content_hash "
            "AND deleted_at IS NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_stale_embedding")
    op.drop_column("memories", "embedded_content_hash")
