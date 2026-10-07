"""Partial indexes for session rollback and the held-memory queue.

g2.9. Two reads the write gate's review needs, and nothing indexed either:

- ``ix_memories_session``: the broker stamps each memory it writes with
  ``metadata.session_id``. Rolling a session back, and listing what it had
  held, look its rows up by that id. PARTIAL on live rows that have one, keyed
  on ``(tenant_id, metadata ->> 'session_id')``, the expression
  ``postgres_service.session_rows_where`` filters on.
- ``ix_memories_held``: the review queue lists a tenant's held memories newest
  first and counts them. PARTIAL on held rows, keyed on
  ``(tenant_id, created_at, id)``, the queue's order, so the queue and its count
  read held rows only.

Write cost: a broker write enters the session index at insert and leaves it on
delete. A held memory enters the held index at insert and leaves it on release
or reject. Other rows enter neither.

Revision ID: 062
Revises: 061
Create Date: 2026-10-07
"""

from collections.abc import Sequence

from alembic import op

from core_storage_api.database.migration_helpers import drop_invalid_indexes

revision: str = "062"
down_revision: str | None = "061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CONCURRENTLY in an autocommit block: ``memories`` is a large table.
    with op.get_context().autocommit_block():
        drop_invalid_indexes("ix_memories_session", "ix_memories_held")
        # One source line for each ``CREATE INDEX ... ON <table>`` prefix, so the
        # CONCURRENTLY guard's regex sees it.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_session ON memories "
            "(tenant_id, (metadata ->> 'session_id')) "
            "WHERE deleted_at IS NULL AND (metadata ->> 'session_id') IS NOT NULL"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_held ON memories "
            "(tenant_id, created_at, id) "
            "WHERE deleted_at IS NULL AND status = 'quarantined'"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_held")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_session")
