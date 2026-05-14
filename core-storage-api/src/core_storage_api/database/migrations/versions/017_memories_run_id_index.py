"""Add partial btree index on ``memories (tenant_id, run_id)`` for ingest
batch queries (undo, parent-Document join, "show all memories from this
upload").

The ``run_id`` column has been on ``memories`` since the original schema
(populated on every ingest commit), but until now nothing queried by it
directly — the ``ingest_undo`` endpoint filtered on
``metadata->>'ingest_run_id'`` (slow JSONB extract; no index) and the
doc-hash cache extracted run_id post-fetch in application code.

After the parent-Document PR, ``run_id`` is the single source of truth for
batch identity (the metadata.ingest_run_id duplicate is dropped from new
writes) and the new ``documents (collection='ingest-sources')`` row joins
back via this column. Without this index those joins + undo queries would
sequential-scan the partition.

Partial: most memories aren't from ingest and have ``run_id IS NULL``;
indexing those rows would waste space without query benefit.

CONCURRENTLY: the bulk of memories live on this table, plain
``CREATE INDEX`` takes an AccessExclusiveLock which blocks all writes
until the build completes. Concurrent build matches the pattern
established in 005/007/011/016.

Revision ID: 017
Revises: 016
Create Date: 2026-05-14
"""

from collections.abc import Sequence

from alembic import op

revision: str = "017"
down_revision: str | None = "016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_memories_run_id "
            "ON memories (tenant_id, run_id) "
            "WHERE run_id IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_run_id")
