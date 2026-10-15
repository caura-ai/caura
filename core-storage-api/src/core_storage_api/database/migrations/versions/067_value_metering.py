"""Value metering: agent sessions, memory origins and memory reuse.

docs/plans/value-metering.md; ``common/models/value_metering.py`` has the
tables' contract. Counts and ids only. ``memory_origins`` and ``memory_reuse``
reference ``memories`` ON DELETE CASCADE, so a purged or hard-deleted memory
takes its rows with it; ``agent_sessions`` joins the tenant purge.

Revision ID: 067
Revises: 066
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "067"
down_revision: str | None = "066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("session_id", sa.Text(), primary_key=True),
        sa.Column("agent_id", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calls", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint("calls >= 0", name="ck_agent_sessions_calls_nonneg"),
    )
    op.create_index("ix_agent_sessions_tenant_first_seen", "agent_sessions", ["tenant_id", "first_seen"])

    op.create_table(
        "memory_origins",
        sa.Column(
            "memory_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("written_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("origin_seconds", sa.Integer(), nullable=True),
        sa.Column("origin_calls", sa.Integer(), nullable=True),
        sa.Column("origin_tokens_in", sa.BigInteger(), nullable=True),
        sa.Column("origin_tokens_out", sa.BigInteger(), nullable=True),
        sa.Column("origin_turns", sa.Integer(), nullable=True),
        sa.Column("origin_source", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "coalesce(origin_seconds, 0) >= 0 AND coalesce(origin_calls, 0) >= 0 "
            "AND coalesce(origin_tokens_in, 0) >= 0 AND coalesce(origin_tokens_out, 0) >= 0 "
            "AND coalesce(origin_turns, 0) >= 0",
            name="ck_memory_origins_nonneg",
        ),
    )
    op.create_index(
        "ix_memory_origins_tenant_session_written",
        "memory_origins",
        ["tenant_id", "session_id", "written_at"],
    )

    op.create_table(
        "memory_reuse",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("session_id", sa.Text(), primary_key=True),
        sa.Column(
            "memory_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("reader_agent_id", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("writer_agent_id", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("writer_session_id", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("first_recalled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_memory_reuse_tenant_recalled", "memory_reuse", ["tenant_id", "first_recalled_at"])
    # A new, empty table: no CONCURRENTLY needed. Backs the cascade from memories.
    op.create_index("ix_memory_reuse_memory", "memory_reuse", ["memory_id"])


def downgrade() -> None:
    op.drop_table("memory_reuse")
    op.drop_table("memory_origins")
    op.drop_table("agent_sessions")
