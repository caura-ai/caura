"""Indexes for the contradiction window and the expiry sweep.

B33 of the 2026-10-01 audit. Two reads of ``memories`` had no index that matched
what they filter on:

- L-185, ``ix_memories_contradicted_window``. ``outcome_contradiction_signals``
  windows on ``COALESCE(status_changed_at, created_at)``: 045 added the column
  with no backfill, so readers fall back to ``created_at``. 045's
  ``ix_memories_status_changed_at`` keyed the bare column, and a btree on a
  column cannot serve a range over an expression of it, so the window was a
  filter over every contradicted row of the tenant. This index keys the
  expression, under 045's predicate, and replaces 045's: that window was its
  only reader.
- L-191, ``ix_memories_expires_at`` and ``ix_memories_valid_end``.
  ``memory_archive_expired`` takes ``ts_valid_end < NOW() OR expires_at <
  NOW()`` among a tenant's live rows. Neither column had an index it could use,
  so each daily run read the tenant's whole live slice. One partial index per
  column, on live rows that have it set, serves each arm, and the planner ORs
  the two. The live statuses are ``LIVE_MEMORY_STATUSES``, which the sweep
  binds, so a row leaves both indexes when the sweep archives it.

Write cost: a contradicted row enters the window index when its status flips,
as it entered 045's. Only a live row with an ``expires_at`` or a
``ts_valid_end`` enters the expiry indexes. None of the three keys or filters on
``recall_count`` or ``last_recalled_at``, the columns a recall bump writes, so a
bump stays a HOT update (063).

Revision ID: 065
Revises: 064
Create Date: 2026-10-08
"""

from collections.abc import Sequence

from alembic import op

from core_storage_api.database.migration_helpers import drop_invalid_indexes

revision: str = "065"
down_revision: str | None = "064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CONCURRENTLY in an autocommit block: ``memories`` is a large table.
    with op.get_context().autocommit_block():
        drop_invalid_indexes(
            "ix_memories_contradicted_window", "ix_memories_expires_at", "ix_memories_valid_end"
        )
        # One source line for each ``CREATE INDEX ... ON <table>`` prefix, so the
        # CONCURRENTLY guard's regex sees it.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_contradicted_window ON memories "
            "(tenant_id, COALESCE(status_changed_at, created_at)) "
            "WHERE status IN ('outdated', 'conflicted')"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_expires_at ON memories "
            "(tenant_id, expires_at) "
            "WHERE deleted_at IS NULL AND status IN ('active', 'confirmed', 'pending') "
            "AND expires_at IS NOT NULL"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_valid_end ON memories "
            "(tenant_id, ts_valid_end) "
            "WHERE deleted_at IS NULL AND status IN ('active', 'confirmed', 'pending') "
            "AND ts_valid_end IS NOT NULL"
        )
        # Last, once the window index that replaces it is in place.
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_status_changed_at")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        drop_invalid_indexes("ix_memories_status_changed_at")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_status_changed_at ON memories "
            "(tenant_id, status_changed_at) "
            "WHERE status IN ('outdated', 'conflicted')"
        )
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_valid_end")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_expires_at")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_contradicted_window")
