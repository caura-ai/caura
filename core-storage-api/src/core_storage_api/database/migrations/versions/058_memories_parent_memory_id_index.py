"""Partial index over memories derived from another memory.

B25 (M-52, M-53). Auto-chunk and atomic-fact children carry their parent's text
and link to it only through ``metadata.parent_memory_id``. Every memory delete
now soft-deletes the live rows derived from what it deletes, in the same
transaction, so the child lookup became part of every delete rather than a
rare remediation query. Nothing served it: each lookup scanned the tenant's
live rows, which is why the single-delete cascade had been gated on markers
that parents written before the cascade existed do not carry.

PARTIAL on live rows that HAVE a parent, so it holds only derived rows: empty
for a store with no auto-chunked or fanned-out content, and a delete looks
children up in O(children) instead of O(live rows in the tenant). The key
expression and predicate must match ``postgres_service.derived_rows_where``
for the planner to use it.

Write cost: a derived row enters the index at insert and leaves it on delete.
Rows without a parent never enter it.

Revision ID: 058
Revises: 057
Create Date: 2026-10-05
"""

from collections.abc import Sequence

from alembic import op

from core_storage_api.database.migration_helpers import drop_invalid_indexes

revision: str = "058"
down_revision: str | None = "057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CONCURRENTLY in an autocommit block: ``memories`` is a large table.
    with op.get_context().autocommit_block():
        drop_invalid_indexes("ix_memories_parent_memory_id")
        # One source line for the ``CREATE INDEX ... ON <table>`` prefix, so the
        # CONCURRENTLY guard's regex sees it.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_parent_memory_id ON memories "
            "(tenant_id, (metadata ->> 'parent_memory_id')) "
            "WHERE deleted_at IS NULL AND (metadata ->> 'parent_memory_id') IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_parent_memory_id")
