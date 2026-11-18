"""``background_model_work.deferrals``: consecutive scheduling deferrals of one item.

A deferral is a scheduling refusal, not a failure, so ``/finish`` refunds its
claim and it never exhausts the replay cap. But it also retried after exactly
the caller's hint, which for a tenant whose model slots are busy is one second:
a check deferred by a full budget came back every second or two for as long as
the budget stayed full, repeating its storage reads each time. The count lets
consecutive deferrals back off instead, and resets once one run gets through.

NOT NULL with a constant default: PostgreSQL 11+ keeps the default in the
catalog, so the table is not rewritten, and every existing row starts at 0.

Revision ID: 081
Revises: 080
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "081"
down_revision: str | None = "080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "background_model_work",
        sa.Column("deferrals", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )


def downgrade() -> None:
    op.drop_column("background_model_work", "deferrals")
