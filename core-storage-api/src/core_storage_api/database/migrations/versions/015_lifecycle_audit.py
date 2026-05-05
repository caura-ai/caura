"""Create the ``lifecycle_audit`` table for CAURA-655.

One row per scheduled lifecycle invocation (per-org, per-action). The
core-api fanout endpoint pre-publishes a row with ``status='pending'``
before each per-org Pub/Sub message; the core-worker consumer flips
the row to ``in_progress`` on receipt and ``success`` / ``failure`` on
completion. The table makes lost (DLQ'd / never-consumed) messages
recoverable: they stay ``pending`` past their expected finish time.

``org_id`` is plain ``text`` (no FK) — same shape as
``organization_settings.org_id`` from CAURA-654 — so the schema works
in pure-OSS (key = standalone tenant id) and enterprise (key = real
org id) deployments without a separate orgs table dependency.

Revision ID: 015
Revises: 014
Create Date: 2026-05-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "015"
down_revision: str | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "lifecycle_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("triggered_by", sa.Text(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("stats", JSONB),
        sa.Column("error_message", sa.Text()),
    )
    op.execute(
        "CREATE INDEX idx_lifecycle_audit_org_action_started "
        "ON lifecycle_audit (org_id, action, started_at DESC)"
    )


def downgrade() -> None:
    op.drop_table("lifecycle_audit")
