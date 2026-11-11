"""Install-bound, minute-coalesced rule fetch records.

Revision ID: 079
Revises: 078
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "079"
down_revision: str | None = "078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "keystone_fetches",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("install_uuid", UUID(as_uuid=True), nullable=False),
        sa.Column("credential_id", sa.Text(), nullable=False),
        sa.Column("principal", sa.Text(), nullable=False),
        sa.Column("fleet_id", sa.Text(), nullable=False),
        sa.Column("rule_set_hash", sa.Text(), nullable=False),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("served_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "tenant_id",
            "install_uuid",
            "principal",
            "fleet_id",
            "rule_set_hash",
            "bucket_start",
            name="uq_keystone_fetch_bucket",
        ),
        sa.CheckConstraint("rule_set_hash ~ '^[0-9a-f]{64}$'", name="ck_keystone_fetch_hash"),
        sa.CheckConstraint(
            "served_at >= bucket_start AND served_at < bucket_start + interval '60 seconds'",
            name="ck_keystone_fetch_time",
        ),
    )
    op.create_index("ix_keystone_fetch_retention", "keystone_fetches", ["served_at"])


def downgrade() -> None:
    op.drop_table("keystone_fetches")
