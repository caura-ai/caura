"""Shared tenant attempt budgets and reference-only background retries.

Revision ID: 080
Revises: 079
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "080"
down_revision: str | None = "079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "background_model_budgets",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint("used >= 0", name="ck_background_model_budget_used"),
    )
    op.create_table(
        "background_model_reservations",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_table(
        "background_model_work",
        sa.Column("tenant_id", sa.Text(collation="C"), primary_key=True),
        sa.Column("kind", sa.Text(), primary_key=True),
        sa.Column("memory_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("terminal_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "terminal_reason IS NULL OR terminal_reason = 'retry_exhausted'",
            name="ck_background_model_work_terminal_reason",
        ),
    )
    op.create_index(
        "ix_background_model_work_due",
        "background_model_work",
        ["available_at", "lease_until"],
        postgresql_where=sa.text("terminal_reason IS NULL"),
    )
    op.create_index(
        "ix_background_model_work_tenant_due",
        "background_model_work",
        ["tenant_id", "available_at", "lease_until"],
        postgresql_where=sa.text("terminal_reason IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("background_model_work")
    op.drop_table("background_model_reservations")
    op.drop_table("background_model_budgets")
