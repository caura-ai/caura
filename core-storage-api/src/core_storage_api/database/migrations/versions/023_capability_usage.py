"""Capability-usage analytics counters (adoption report).

Append-only aggregate table behind the "which capabilities get used, by
transport, per org" adoption report. core-api's in-process aggregator
collapses many requests into one row per
``(tenant_id, capability, op, transport, ts_bucket)`` and flushes every
~15s. Multiple instances each append their own rows; consumers SUM at
query time — hence NO unique constraint and NO upsert (append-only,
contention-free).

Deliberately CROSS-TENANT analytics: RLS is intentionally NOT enabled on
this table (the report aggregates adoption across orgs). It holds only
counts + latency sums and a ``tenant_id`` grouping dimension — no memory
content. Read access is granted out-of-band to the analytics reader role
(kept out of this migration so no environment-specific role name is
baked into the public schema).

Revision ID: 023
Revises: 022
Create Date: 2026-06-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "023"
down_revision: str | None = "022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "capability_usage",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("capability", sa.Text(), nullable=False),
        sa.Column("op", sa.Text(), nullable=True),
        sa.Column("transport", sa.Text(), nullable=False),
        sa.Column("ts_bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "duration_ms_sum",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "transport IN ('mcp', 'rest')",
            name="ck_capability_usage_transport",
        ),
    )
    op.execute("CREATE INDEX ix_capability_usage_bucket ON capability_usage (ts_bucket)")
    op.execute("CREATE INDEX ix_capability_usage_tenant_bucket ON capability_usage (tenant_id, ts_bucket)")


def downgrade() -> None:
    op.drop_table("capability_usage")
