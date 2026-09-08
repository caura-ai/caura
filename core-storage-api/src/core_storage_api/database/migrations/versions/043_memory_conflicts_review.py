"""Human review state on ``memory_conflicts`` (D11).

``memory_conflicts`` records what the DETECTOR concluded — the relationship, the
diagnosis, the action it proposed. Nothing records what a PERSON concluded, so
there is no way to ask the only question that matters for precision: how often
was the detector right?

These columns keep the two apart deliberately. ``action``/``audit_reason`` stay
the detector's proposal; ``resolution_action``/``resolution_note`` are the
reviewer's decision, drawn from the SAME ``ACTIONS`` vocabulary so "did the human
agree" is a direct comparison rather than a mapping exercise. A ``dismissed`` row
is the most valuable of the three states: it is the only place in the system that
records a FALSE POSITIVE.

``review_status`` is NOT NULL DEFAULT 'pending'. On PostgreSQL 11+ adding a
column with a non-volatile default is a catalog-only change — no table rewrite —
so this is safe without the NOT VALID / VALIDATE two-step, and
``memory_conflicts`` is not one of the repo's declared large tables in any case.

The queue index is ``(tenant_id, review_status, created_at)`` in that order: the
review queue is always "this tenant's pending rows, oldest first", so tenant and
status are equality predicates and created_at supplies the ordering. Without it
the queue degrades to a seq scan, and this table grows on every detected
conflict.

Revision ID: 043
Revises: 042
Create Date: 2026-09-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "043"
down_revision: str | None = "042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CK_STATUS = "ck_memory_conflicts_review_status"
_CK_ACTION = "ck_memory_conflicts_resolution_action"
_IX_QUEUE = "ix_memory_conflicts_review_queue"

_REVIEW_STATUSES = ("pending", "resolved", "dismissed")
# Mirrors common.models.memory_conflict.ACTIONS. Duplicated as a literal on
# purpose: a migration must describe the schema at ITS point in history, not
# import a constant that a later release can change underneath it.
_ACTIONS = (
    "replace",
    "supersede",
    "scope",
    "merge",
    "split_entity",
    "downweight",
    "mark_disputed",
    "ask",
    "no_op",
)


def _in_list(col: str, values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values) and f"{col} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE memory_conflicts ADD COLUMN IF NOT EXISTS review_status TEXT NOT NULL DEFAULT 'pending'"
    )
    for col in ("resolution_action", "resolution_note", "resolved_by"):
        op.execute(f"ALTER TABLE memory_conflicts ADD COLUMN IF NOT EXISTS {col} TEXT")
    op.execute("ALTER TABLE memory_conflicts ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ")

    op.execute(
        f"ALTER TABLE memory_conflicts ADD CONSTRAINT {_CK_STATUS} "
        f"CHECK ({_in_list('review_status', _REVIEW_STATUSES)})"
    )
    # Nullable: a pending row has no resolution_action yet.
    op.execute(
        f"ALTER TABLE memory_conflicts ADD CONSTRAINT {_CK_ACTION} "
        f"CHECK (resolution_action IS NULL OR {_in_list('resolution_action', _ACTIONS)})"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_IX_QUEUE} ON memory_conflicts (tenant_id, review_status, created_at)"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_IX_QUEUE}")
    op.execute(f"ALTER TABLE memory_conflicts DROP CONSTRAINT IF EXISTS {_CK_ACTION}")
    op.execute(f"ALTER TABLE memory_conflicts DROP CONSTRAINT IF EXISTS {_CK_STATUS}")
    for col in ("resolved_at", "resolved_by", "resolution_note", "resolution_action", "review_status"):
        op.execute(f"ALTER TABLE memory_conflicts DROP COLUMN IF EXISTS {col}")
