"""``memory_exposure``: which agents a memory was handed to.

Correction propagation (``docs/plans/correction-propagation.md``), D1;
``common/models/memory_exposure.py`` has the table's contract. One row per
(tenant, memory, agent) with first/last seen, a read count and the latest
read's trace. Ids and counts only. ``memory_id`` references ``memories`` ON
DELETE CASCADE, so a purged or hard-deleted memory takes its rows with it.

A new, empty table in one transaction: no lock on ``memories`` beyond the FK's
brief SHARE ROW EXCLUSIVE, and no CONCURRENTLY needed for its own index.

Revision ID: 072
Revises: 071
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "072"
down_revision: str | None = "071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_exposure",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column(
            "memory_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("agent_id", sa.Text(), primary_key=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("count", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("last_trace_id", sa.Text(), nullable=True),
        sa.CheckConstraint("count >= 1", name="ck_memory_exposure_count_positive"),
    )
    # Backs the cascade from memories.
    op.create_index("ix_memory_exposure_memory", "memory_exposure", ["memory_id"])


def downgrade() -> None:
    op.drop_table("memory_exposure")
