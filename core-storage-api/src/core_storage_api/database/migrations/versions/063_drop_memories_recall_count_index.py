"""Drop ``ix_memories_recall_count`` so a recall bump can be a HOT update.

M-111. Every agent-identified search bumps ``recall_count`` and
``last_recalled_at`` on the rows it returns (``memory_increment_recall``).
PostgreSQL applies an UPDATE as a heap-only tuple (HOT) update, with no new
index entries, only when no column it writes is indexed. This standalone btree
from 001 keyed ``recall_count``, so no bump could be HOT: each bumped row got a
new entry in every index on ``memories``, and in the HNSW index each insert
runs a neighbour search for the unchanged vector. ``last_recalled_at`` has no
index, so without this one the bump writes no indexed column.

NOTHING NEEDS IT. Every query on ``recall_count`` also filters on a tenant (the
quality metrics, the insights queries, the stale-memory archiving), and the
tenant-leading indexes serve those. The exception is the cross-tenant admin
list sorted by ``recall_count``, which now reads and sorts the rows itself, as
its sort by ``weight`` already does.

``DROP INDEX CONCURRENTLY`` in an autocommit block, as in 008: ``memories`` is a
large, busy table, and a plain DROP INDEX takes an ACCESS EXCLUSIVE lock on it.
The downgrade rebuilds the index concurrently.

Revision ID: 063
Revises: 062
Create Date: 2026-10-07
"""

from collections.abc import Sequence

from alembic import op

from core_storage_api.database.migration_helpers import drop_invalid_indexes

revision: str = "063"
down_revision: str | None = "062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_recall_count")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        drop_invalid_indexes("ix_memories_recall_count")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_recall_count ON memories (recall_count)"
        )
