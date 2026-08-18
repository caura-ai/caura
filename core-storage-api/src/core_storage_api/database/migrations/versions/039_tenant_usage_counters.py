"""Durable per-tenant, per-period usage counters (billing-grade).

The exact counterpart to ``capability_usage`` (023), and deliberately a
separate table rather than a change to it. That one is adoption analytics:
core-api buffers in memory and flushes every ~15s, so counts since the last
flush are lost if the process dies, and it appends rows to be SUMmed with no
unique constraint. Both properties are right for "which capabilities get used"
and wrong for anything a plan cap is computed from — a lost flush is a customer
under-billed or over-served, and an unbounded append is a growing SUM on the
read path.

This table upserts instead: one row per ``(tenant_id, operation,
period_start)``, incremented atomically by
``INSERT … ON CONFLICT DO UPDATE SET count = count + excluded.count``. No
buffering, no loss window, bounded row count, and a period total that is a
single-row read.

ROW PER OPERATION, not a column per operation. The platform's
``enterprise.usage_counters`` has ``writes``/``searches``/``recalls`` columns,
which is why ``insights`` and ``evolve`` — both metered operations in core-api
— have nowhere to go there. A row keyed by the operation name takes a new
operation without a migration.

Cross-tenant by design, like 023: it holds counts and a ``tenant_id`` grouping
dimension, no memory content. The platform reads it directly — both schemas
live in the same database, so aggregating a plan cap costs no network hop.

Part of caura-ai/caura-enterprise#83. The write path reaches this through
``ServiceHooks.usage_meter`` (added in #824), so an OSS standalone deployment
with no meter wired still records nothing and keeps no limits.

Revision ID: 039
Revises: 038
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "039"
down_revision: str | None = "038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_usage_counters",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        # Start of the billing period the count belongs to, UTC. The caller
        # supplies it (truncated to the month) rather than the database
        # deriving it, so a row cannot land in a different period than the one
        # the caller believes it is counting — and so a backfill can target a
        # closed period explicitly.
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # The upsert target. Without it ``ON CONFLICT`` has nothing to bind to and
    # concurrent writers silently create duplicate rows for one period —
    # exactly the append-and-SUM shape this table exists to avoid.
    op.execute(
        "CREATE UNIQUE INDEX uq_tenant_usage_counters_key "
        "ON tenant_usage_counters (tenant_id, operation, period_start)"
    )
    # The read a plan cap actually makes: everything for one tenant in one
    # period. Covered by the unique index's leading columns for the
    # tenant-scoped case, but the platform also sweeps a whole period across
    # tenants, which wants period_start leading.
    op.execute("CREATE INDEX ix_tenant_usage_counters_period ON tenant_usage_counters (period_start)")


def downgrade() -> None:
    op.drop_table("tenant_usage_counters")
