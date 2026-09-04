"""``tenant_usage_counters.count`` >= 0.

Migration 039 created the column as plain ``BigInteger NOT NULL DEFAULT 0`` with
nothing forbidding a negative, and the upsert behind it adds rather than assigns
(``count = count + excluded.count``). So any negative reaching the increment path
does not merely record a smaller number — it drives the stored counter down, and
a counter below zero reads as vastly UNDER budget in the platform's
``_is_over_plan_limits`` (``used > limit``). The effect is that plan enforcement
silently switches off for that tenant, which is the opposite of what a usage
counter going wrong should do.

The router now rejects a negative ``count`` at the edge (422). This constraint is
the floor beneath that check, and it covers the case the edge cannot: a hand-run
correction against the table, which is exactly how the gap was noticed.

CLAMP BEFORE CONSTRAIN. Migrations here run in the FastAPI lifespan startup hook
(see 038), so a migration that fails is a deploy that never becomes ready. Adding
a validated CHECK to a table that already holds a negative row would do precisely
that. Any such row is clamped to 0 first.

Clamping to 0 rather than to a reconstructed value is deliberate: the true count
is not recoverable — the table stores a running total, not the history that
produced it — and 0 is both the documented floor and the conservative direction
for enforcement, since it raises the tenant back toward their limit rather than
leaving them under it. A clamped row is identifiable afterwards by an
``updated_at`` matching this migration's run.

``tenant_usage_counters`` is not one of the repo's declared large tables (see
``test_no_plain_set_not_null_on_large_tables``) — it is bounded at one row per
tenant per operation per period by the unique index 039 added — so the plain
validated ``ADD CONSTRAINT`` is used rather than 038's NOT VALID / VALIDATE
two-step. The clamp and the add share one transaction: the add takes
AccessExclusive, so a concurrent writer cannot slip a negative in between them.

Revision ID: 042
Revises: 041
Create Date: 2026-09-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "042"
down_revision: str | None = "041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_tenant_usage_counters_count_nonneg"


def upgrade() -> None:
    # Row locks on the offending rows only. Normally a no-op.
    op.execute("UPDATE tenant_usage_counters SET count = 0, updated_at = now() WHERE count < 0")
    op.execute(f"ALTER TABLE tenant_usage_counters ADD CONSTRAINT {_CONSTRAINT} CHECK (count >= 0)")


def downgrade() -> None:
    # The clamp is not undone: the pre-migration values were unrecoverable when
    # they were overwritten and remain so now.
    op.execute(f"ALTER TABLE tenant_usage_counters DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
