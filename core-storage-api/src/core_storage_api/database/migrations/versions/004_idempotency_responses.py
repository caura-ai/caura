"""Idempotency-Key inbox table (CAURA-601).

Backs the reconcile-on-timeout path (CAURA-599) and the public
Idempotency-Key header contract. A row per (tenant_id, key) stores
the request hash + the response to replay.

Revision ID: 004
Revises: 003
Create Date: 2026-04-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "idempotency_responses",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column(
            "response_body",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "idempotency_key", name="pk_idempotency_responses"),
    )
    # Periodic cleanup scans by expires_at; btree index is sufficient.
    op.create_index(
        "ix_idempotency_responses_expires_at",
        "idempotency_responses",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_idempotency_responses_expires_at",
        table_name="idempotency_responses",
    )
    op.drop_table("idempotency_responses")
