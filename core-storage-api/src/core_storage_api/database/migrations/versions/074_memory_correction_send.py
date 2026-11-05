"""Sending a correction: delivery state on ``memory_correction`` and its recipients.

Correction propagation (``docs/plans/correction-propagation.md``), CP-11 and
CP-15; ``common/models/memory_correction.py`` has the tables' contract.

- ``memory_correction.status`` widens from ``preview|sent`` to
  ``preview|sending|sent|closed``, and the record gains ``sent_at``,
  ``sent_by`` and ``bus_checked_at`` (when the bus delivery states were last
  read, so a status read refreshes them at most every 30 s).
- ``memory_correction_recipient`` gains ``channel`` (``bus`` or ``recall``,
  null until sent), ``confirmed_at``, ``notified_at``, ``next_attempt_at`` and
  ``attempts`` (a throttled send waits), and the bus's ``message_id`` and
  ``delivery_id``. ``notice_state`` gets the CHECK 073 left to this revision.
- ``ix_memory_correction_recipient_agent`` serves "notices for agent X", the
  recall fallback (CP-13).

Both tables are small (at most 50 recipients per correction) and only a day
old, so the CHECKs validate in place and the index builds in the transaction.

Revision ID: 074
Revises: 073
Create Date: 2026-11-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "074"
down_revision: str | None = "073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOTICE_STATES = "'pending', 'sent', 'delivered', 'confirmed', 'late', 'failed', 'unreachable'"


def upgrade() -> None:
    op.drop_constraint("ck_memory_correction_status", "memory_correction", type_="check")
    op.create_check_constraint(
        "ck_memory_correction_status",
        "memory_correction",
        "status IN ('preview', 'sending', 'sent', 'closed')",
    )
    op.add_column("memory_correction", sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("memory_correction", sa.Column("sent_by", sa.Text(), nullable=True))
    op.add_column("memory_correction", sa.Column("bus_checked_at", sa.DateTime(timezone=True), nullable=True))

    recipient = "memory_correction_recipient"
    op.add_column(recipient, sa.Column("channel", sa.Text(), nullable=True))
    op.add_column(recipient, sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(recipient, sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(recipient, sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(recipient, sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column(recipient, sa.Column("bus_message_id", sa.Text(), nullable=True))
    op.add_column(recipient, sa.Column("bus_delivery_id", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_memory_correction_recipient_notice_state", recipient, f"notice_state IN ({_NOTICE_STATES})"
    )
    op.create_check_constraint(
        "ck_memory_correction_recipient_channel", recipient, "channel IN ('bus', 'recall')"
    )
    # Last, so the head fingerprint can probe it.
    op.create_index("ix_memory_correction_recipient_agent", recipient, ["tenant_id", "agent_id"])


def downgrade() -> None:
    recipient = "memory_correction_recipient"
    op.drop_index("ix_memory_correction_recipient_agent", table_name=recipient)
    op.drop_constraint("ck_memory_correction_recipient_channel", recipient, type_="check")
    op.drop_constraint("ck_memory_correction_recipient_notice_state", recipient, type_="check")
    for column in (
        "bus_delivery_id",
        "bus_message_id",
        "attempts",
        "next_attempt_at",
        "notified_at",
        "confirmed_at",
        "channel",
    ):
        op.drop_column(recipient, column)
    for column in ("bus_checked_at", "sent_by", "sent_at"):
        op.drop_column("memory_correction", column)
    # A record already past ``preview`` keeps the only state 073 knows for it.
    op.execute("UPDATE memory_correction SET status = 'sent' WHERE status IN ('sending', 'closed')")
    op.drop_constraint("ck_memory_correction_status", "memory_correction", type_="check")
    op.create_check_constraint(
        "ck_memory_correction_status", "memory_correction", "status IN ('preview', 'sent')"
    )
