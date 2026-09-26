"""memories.status_changed_at — when the row's status last changed

09/02 M-55. A contradiction is a status FLIP on an existing memory, and that
event had no timestamp anywhere on the row. ``outcome_contradiction_signals``
therefore had to window on ``created_at``, so a memory written weeks ago and
contradicted today fell outside the current scan window and its failure
evidence was dropped — silently, because "no rows" and "no contradictions" are
indistinguishable to the caller.

The column is NULLABLE with no backfill, deliberately. There is no source of
truth for when a historical row's status changed; inventing one (created_at,
or the migration timestamp) would fabricate evidence and shift old failures
into whatever window happened to run next. Readers COALESCE to ``created_at``,
which reproduces today's behaviour exactly for every pre-existing row and is
correct for every row written from here on.

Revision ID: 045
Revises: 044
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "045"
down_revision: str | Sequence[str] | None = "044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEX_NAME = "ix_memories_status_changed_at"


def upgrade() -> None:
    op.add_column(
        "memories",
        sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    # ``CONCURRENTLY`` cannot run inside a transaction, hence the autocommit
    # block — same shape as 040/041. The DROP first clears an INVALID index
    # left behind by an interrupted earlier build: CONCURRENTLY waits on
    # concurrent transactions, and a build killed while waiting leaves the
    # index in ``pg_index`` unusable but present, so ``IF NOT EXISTS`` alone
    # would skip the rebuild forever. See 040 for the full reasoning.
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
        # PARTIAL, matching the analytic query's shape: it only ever reads
        # contradicted rows, a small fraction of the table. Indexing the whole
        # column would cost write throughput on every status change, for rows
        # no reader of this column looks at.
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
            "ON memories (tenant_id, status_changed_at) "
            "WHERE status IN ('outdated', 'conflicted')"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
    op.drop_column("memories", "status_changed_at")
