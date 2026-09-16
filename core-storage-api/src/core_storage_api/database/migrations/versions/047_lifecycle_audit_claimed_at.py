"""Add ``claimed_at`` / ``claim_token`` so the pending -> in_progress transition is single-winner.

``lifecycle_audit_finalize`` previously guarded only ``status != 'success'``,
so nothing stopped two deliveries of the same ``audit_id`` from both moving a
row out of ``pending`` and running the underlying primitive concurrently. That
became reachable with the reconcile sweep: a message merely delayed behind a
backlog leaves its row ``pending`` exactly like a message that was never
published, and subscriptions here retain messages for seven days, so no
practical age threshold can separate the two. For ``crystallize`` or
``insights`` a concurrent pair is duplicate LLM spend and duplicate records.

``claim_token`` is what keeps the guard from misfiring on its own client. The
storage client retries a PATCH on ``ReadTimeout`` and 5xx, which was harmless
while the write was idempotent; against a compare-and-swap, a retry of a claim
that already succeeded would look like a competing consumer and make the
handler nack a delivery it had won. The token is one value per consumer
invocation, so a retry matches and a genuine second delivery does not.

Both nullable, because NULL is the correct resting state for a row that has
never been claimed. It is NOT correct for a row that is already
``in_progress`` when this migration lands. The CAS admits a NULL
``claimed_at`` as "free to claim", so a genuinely running operation would be
stealable by a concurrent delivery for the whole deploy window -- the exact
duplicate run this migration exists to prevent, reachable precisely while it
is being rolled out.

Those rows are therefore stamped with ``now()``. That starts the ordinary
lease against them rather than exempting them: a live consumer keeps its row
for the lease, and one that has already died releases it on expiry through
the same staleness arm as any other abandoned claim, so nothing is stranded.

Adding nullable columns with no default is metadata-only in Postgres. The
backfill matches only rows currently ``in_progress`` -- normally none, and at
most a handful -- so this still does not rewrite the table.

Revision ID: 047
Revises: 046
Create Date: 2026-09-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "047"
down_revision: str | None = "046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "lifecycle_audit",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "lifecycle_audit",
        sa.Column("claim_token", sa.Text(), nullable=True),
    )
    # Rows mid-flight at this moment have no claim to read back, and an
    # unstamped ``in_progress`` row is claimable by anyone. Give them a lease
    # starting now so the deploy window is not the one window in which the
    # guarantee does not hold. ``claim_token`` is deliberately left NULL: the
    # original consumer has no token to re-present, and a NULL token matches
    # no competitor either.
    op.execute(
        sa.text(
            "UPDATE lifecycle_audit SET claimed_at = now() "
            "WHERE status = 'in_progress' AND claimed_at IS NULL"
        )
    )


def downgrade() -> None:
    op.drop_column("lifecycle_audit", "claim_token")
    op.drop_column("lifecycle_audit", "claimed_at")
