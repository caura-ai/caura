"""``baton_recipient``: which agents a handoff baton is waiting on.

Handoff batons (``docs/plans/handoff-batons.md``), D4, the recall fallback;
``common/models/baton_recipient.py`` has the table's contract. The bus writes
one row per receiver when it sends a baton; a recall shows the rows still
``delivered`` (``baton_notices``) until the receiver accepts or 7 days pass.

A new, empty table in one transaction: its index builds in place, no
CONCURRENTLY needed. No foreign key: the baton lives in the bus and its
``batons`` document, not in a table here.

Revision ID: 076
Revises: 075
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "076"
down_revision: str | None = "075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "baton_recipient",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("baton_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", sa.Text(), primary_key=True),
        sa.Column("from_agent", sa.Text(), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("message_id", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), server_default=sa.text("'delivered'"), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_via", sa.Text(), nullable=True),
        sa.Column("bus_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("state IN ('delivered', 'accepted', 'replied')", name="ck_baton_recipient_state"),
        sa.CheckConstraint("accepted_via IN ('recall', 'bus')", name="ck_baton_recipient_accepted_via"),
    )
    # Last, so the head fingerprint can probe it.
    op.create_index(
        "ix_baton_recipient_waiting",
        "baton_recipient",
        ["tenant_id", "agent_id", "created_at"],
        postgresql_where=sa.text("state = 'delivered'"),
    )


def downgrade() -> None:
    op.drop_index("ix_baton_recipient_waiting", table_name="baton_recipient")
    op.drop_table("baton_recipient")
