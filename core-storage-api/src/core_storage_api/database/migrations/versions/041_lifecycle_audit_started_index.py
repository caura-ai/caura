"""Index lifecycle audit rows by recency for cross-org summaries.

The lifecycle smoke summary filters the append-only ``lifecycle_audit`` table
by ``started_at`` before grouping across every organization. The existing
``(org_id, action, started_at)`` index cannot serve that predicate because its
leading columns are unconstrained. Build this index online so adding the
observation endpoint does not block lifecycle writers on an established
deployment.

Revision ID: 041
Revises: 040
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "041"
down_revision: str | None = "040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "idx_lifecycle_audit_started_at"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        connection = op.get_context().connection
        if connection is None:
            raise RuntimeError("online migration requires a connection")
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
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_lifecycle_audit_started_at ON lifecycle_audit (started_at DESC)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
