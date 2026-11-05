"""``memory_correction`` + ``memory_correction_recipient``: who to tell.

Correction propagation (``docs/plans/correction-propagation.md``), CP-10;
``common/models/memory_correction.py`` has the tables' contract. One record per
``caura.memory.corrected`` event (``event_id`` unique, so a redelivery is a
no-op) and one recipient row per agent that read the memory before it was
corrected. Both cascade from their parent, so a hard-deleted memory takes its
corrections with it.

Two new, empty tables in one transaction: no lock on ``memories`` beyond the
FK's brief SHARE ROW EXCLUSIVE, and no CONCURRENTLY needed for their indexes.

Revision ID: 073
Revises: 072
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "073"
down_revision: str | None = "072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_correction",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column(
            "memory_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("actor_kind", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=True),
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("old_summary", sa.Text(), nullable=True),
        sa.Column("new_summary", sa.Text(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'preview'"), nullable=False),
        sa.Column("truncated", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind IN ('content_edit', 'status', 'unverify', 'conflict_resolve')",
            name="ck_memory_correction_kind",
        ),
        sa.CheckConstraint("status IN ('preview', 'sent')", name="ck_memory_correction_status"),
        sa.CheckConstraint("truncated >= 0", name="ck_memory_correction_truncated"),
    )
    op.create_index(
        "ix_memory_correction_memory", "memory_correction", ["tenant_id", "memory_id", "corrected_at"]
    )
    # Backs the cascade from memories.
    op.create_index("ix_memory_correction_memory_id", "memory_correction", ["memory_id"])

    op.create_table(
        "memory_correction_recipient",
        sa.Column(
            "correction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memory_correction.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("agent_id", sa.Text(), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("reads", sa.BigInteger(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_trace_id", sa.Text(), nullable=True),
        sa.Column("notice_state", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
    )
    op.create_index("ix_memory_correction_recipient_tenant", "memory_correction_recipient", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("memory_correction_recipient")
    op.drop_table("memory_correction")
