"""Add tenant_settings + tenant_settings_audit tables.

Revision ID: 002
Revises: 001
Create Date: 2026-04-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Live tenant overrides — one row per tenant, JSONB holds only overrides.
    op.create_table(
        "tenant_settings",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("settings", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # Append-only audit trail: one row per PUT /settings that actually changed values.
    op.create_table(
        "tenant_settings_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.Text()),  # user id from auth; NULL for system writes
        sa.Column("diff", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(
        "CREATE INDEX idx_tenant_settings_audit_tenant_created "
        "ON tenant_settings_audit (tenant_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_tenant_settings_audit_tenant_created")
    op.drop_table("tenant_settings_audit")
    op.drop_table("tenant_settings")
