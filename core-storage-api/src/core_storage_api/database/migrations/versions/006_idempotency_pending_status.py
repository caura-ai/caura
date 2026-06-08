"""Add ``is_pending`` flag to idempotency_responses for race-free claim.

Closes the concurrency gap documented in
``core-api/src/core_api/middleware/idempotency.py``: two requests
arriving with the same ``Idempotency-Key`` within milliseconds both
miss the cache, both run the handler, both persist database rows.
The row-level claim (INSERT ... ON CONFLICT DO NOTHING) needs a way
to distinguish "I just inserted a placeholder" from "the response is
already cached", which is what ``is_pending`` provides.

Existing rows are completed responses, so the column defaults to
``false`` and back-fills accordingly.

Revision ID: 006
Revises: 005
Create Date: 2026-04-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "idempotency_responses",
        sa.Column(
            "is_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("idempotency_responses", "is_pending")
