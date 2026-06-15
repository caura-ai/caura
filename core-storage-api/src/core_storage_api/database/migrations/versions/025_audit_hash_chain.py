"""Tamper-evident linked-hash chain for audit_log (eToro governance).

Adds the per-tenant chain columns (``seq``, ``prev_hash``, ``event_hash``) to
``audit_log`` plus a dedicated ``audit_chain_head`` serialization-point table.

The chain columns are NULLABLE on purpose: existing append-only rows predate
the chain and are intentionally left unchained — the chain genesis is the
migration boundary. Backfilling a tamper-evident log would launder unprovable
history into a valid-looking chain, so we don't; pre-migration rows stay
queryable but are excluded from verification by the partial unique index
(``WHERE seq IS NOT NULL``).

``ADD COLUMN`` of nullable columns with no default is metadata-only in
PostgreSQL 11+ (no table rewrite, no long lock), so this is safe to run inside
the migration transaction under the lifespan advisory lock even on a large
prod ``audit_log``.

Revision ID: 025
Revises: 024
Create Date: 2026-06-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "025"
down_revision: str | None = "024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("seq", sa.BigInteger(), nullable=True))
    op.add_column("audit_log", sa.Column("prev_hash", sa.LargeBinary(), nullable=True))
    op.add_column("audit_log", sa.Column("event_hash", sa.LargeBinary(), nullable=True))
    # Partial indexes over chained rows only — pre-migration rows (seq IS NULL)
    # are excluded, so legacy append-only history neither bloats the index nor
    # collides on the unique constraint.
    op.execute(
        "CREATE UNIQUE INDEX uq_audit_log_tenant_seq ON audit_log (tenant_id, seq) WHERE seq IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_audit_log_tenant_event_hash "
        "ON audit_log (tenant_id, event_hash) WHERE event_hash IS NOT NULL"
    )
    op.create_table(
        "audit_chain_head",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("last_seq", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("audit_chain_head")
    op.execute("DROP INDEX IF EXISTS ix_audit_log_tenant_event_hash")
    op.execute("DROP INDEX IF EXISTS uq_audit_log_tenant_seq")
    op.drop_column("audit_log", "event_hash")
    op.drop_column("audit_log", "prev_hash")
    op.drop_column("audit_log", "seq")
