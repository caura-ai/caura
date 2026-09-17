"""Index the stranded-row predicate for the lifecycle reconcile sweep.

The sweep looks for rows still at ``status='pending'`` past their expected
finish. ``started_at < now() - interval`` matches nearly the whole
append-only table, so ``idx_lifecycle_audit_started_at`` (revision 041)
would scan almost all of it to return the handful of rows that never
advanced. Making the index partial on the status inverts that: it holds
only rows currently at ``pending``, which is normally zero, so the hourly
sweep costs an index lookup rather than a growing scan.

Build it online so adding the sweep does not block lifecycle writers on an
established deployment.

Revision ID: 046
Revises: 045
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "046"
down_revision: str | None = "045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "idx_lifecycle_audit_stranded"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        connection = op.get_context().connection
        if connection is None:
            raise RuntimeError("online migration requires a connection")
        # A previous CONCURRENTLY build that was interrupted leaves an
        # invalid index behind. It still costs writes and never serves
        # reads, and CREATE ... IF NOT EXISTS would keep it, so drop it
        # first and rebuild. Same guard as revision 041.
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
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lifecycle_audit_stranded "
            "ON lifecycle_audit (started_at) WHERE status = 'pending'"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
