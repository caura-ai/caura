"""Record which agent wrote a document.

ax-0917-m-14. ``documents`` had no author column and ``DocWriteRequest`` is
``extra="forbid"``, so there was no field to send one in either. A fleet whose
agents all write to the same collection could not answer "who wrote this",
which is the question every other row in this schema can answer — ``memories``
has carried ``agent_id`` since the beginning.

The probe that found this worked around it by putting ``owner`` inside
``data``. That is worse than it looks: ``data`` is replaced wholesale on every
upsert, so the attribution survives only as long as each writer remembers to
re-send it, and it is invisible to any query that does not know the convention.

Nullable, and no backfill. Every existing row was written before there was an
author to record, and inventing one — the tenant's first agent, a literal
``"unknown"`` — would be a fabricated fact that reads exactly like a real one.
NULL says "we do not know", which is true.

``ix_documents_tenant_agent`` mirrors ``ix_memories_tenant_agent``: "what has
this agent written" is the query the column exists to serve, and without an
index it scans the tenant's whole document set.

CONCURRENTLY, in an autocommit block, matching 049 — ``documents`` is
populated in any deployment old enough to run this, and a plain CREATE INDEX
holds an AccessExclusiveLock plus the migration advisory lock for the whole
build. ADD COLUMN of a nullable column with no default is metadata-only in
PostgreSQL 11+, so the column itself needs no such care.

Revision ID: 050
Revises: 049
Create Date: 2026-09-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "050"
down_revision: str | None = "049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("agent_id", sa.Text(), nullable=True))
    with op.get_context().autocommit_block():
        # One source line, so the CONCURRENTLY guard's regex sees the clause.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_documents_tenant_agent ON documents (tenant_id, agent_id)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_documents_tenant_agent")
    op.drop_column("documents", "agent_id")
