"""Partial index over memories that still have background work pending.

lme-0929-m-03 (SIDE-56). A LongMemEval store ingested through
``/memories/bulk`` kept changing for days after the write returned: embedding,
enrichment and atomic-fact fan-out all run asynchronously, and nothing told a
caller when they were done. ``GET /memories/stats`` now reports a ``pending``
block and ``settled`` flag computed from durable row state (see
``common.models.memory.PENDING_WORK_SQL``).

No existing index serves that count. Without one it is a heap scan of every
live row in the tenant on each poll — the same order of cost as the stats
breakdown itself, paid again. This index is PARTIAL on the pending predicate,
so it contains only rows with outstanding work: empty once a store settles,
and the count becomes O(pending rows).

Write cost: a row enters the index at insert when it is written pending and
leaves it when the last marker clears. Once settled, a row costs nothing here.
The predicate reads ``metadata``, so a metadata-only UPDATE is no longer
HOT-eligible; the enrichment PATCH was already non-HOT (it rewrites ``title``,
which feeds the indexed ``search_vector``), and the embed PATCH writes the
HNSW-indexed ``embedding``.

Revision ID: 053
Revises: 052
Create Date: 2026-09-29
"""

from collections.abc import Sequence

from alembic import op

from core_storage_api.database.migration_helpers import drop_invalid_indexes

revision: str = "053"
down_revision: str | None = "052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CONCURRENTLY in an autocommit block: ``memories`` is a large table.
    # The predicate is spelled out rather than imported from the model so the
    # migration stays frozen if the model constant later moves; it must match
    # ``common.models.memory.PENDING_WORK_SQL`` for the planner to use it.
    with op.get_context().autocommit_block():
        drop_invalid_indexes("ix_memories_pending_work")
        # One source line for the ``CREATE INDEX ... ON <table>`` prefix, so the
        # CONCURRENTLY guard's regex sees it.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_pending_work ON memories (tenant_id) "
            "WHERE deleted_at IS NULL AND ("
            "embedding IS NULL "
            "OR COALESCE(metadata -> '_system' ->> 'enrichment_pending', "
            "metadata ->> 'enrichment_pending') = 'true' "
            "OR (metadata ->> 'atomic_facts') IS NOT NULL)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_pending_work")
