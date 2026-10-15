"""Per-minute model-call and savings totals, and the flush idempotency key.

Usage metering (``common.usage``): core-api and core-worker add up every model
call — provider-reported tokens in, out and reasoning, per tenant, agent,
fleet, operation, provider and model — plus the savings measures Caura can
stand behind, in memory, and flush them here every ~15 s. See
``common/models/usage_minutes.py`` for the tables' contract.

Per-minute totals rather than a row per call: every memory write makes about
five model calls, so a row per call would be ~10M rows per 2M memories. A
minute is fine enough for a daily report and a spend chart.

``usage_flush_batches`` makes a flush idempotent. ``tenant_usage_counters``
(039) has no such key, which is why its writer must drop a batch whose reply
was lost rather than risk counting it twice (OSS 09/02 L-44); this writer can
re-send the same batch instead.

Cross-tenant and RLS-free like 039: counts only, never content.

Revision ID: 066
Revises: 065
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "066"
down_revision: str | None = "065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_usage_minutes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("minute", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("fleet_id", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("calls", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("failures", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("tokens_in", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("tokens_out", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("tokens_reasoning", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint(
            "calls >= 0 AND failures >= 0 AND tokens_in >= 0 AND tokens_out >= 0 AND tokens_reasoning >= 0",
            name="ck_llm_usage_minutes_nonneg",
        ),
    )
    op.create_index(
        "uq_llm_usage_minutes_key",
        "llm_usage_minutes",
        ["minute", "tenant_id", "agent_id", "fleet_id", "operation", "kind", "provider", "model"],
        unique=True,
    )
    op.create_index("ix_llm_usage_minutes_tenant_minute", "llm_usage_minutes", ["tenant_id", "minute"])

    op.create_table(
        "value_usage_minutes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("minute", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("fleet_id", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("operation", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("quantity", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint("quantity >= 0", name="ck_value_usage_minutes_nonneg"),
    )
    op.create_index(
        "uq_value_usage_minutes_key",
        "value_usage_minutes",
        ["minute", "tenant_id", "agent_id", "fleet_id", "operation", "metric"],
        unique=True,
    )
    op.create_index("ix_value_usage_minutes_tenant_minute", "value_usage_minutes", ["tenant_id", "minute"])

    op.create_table(
        "usage_flush_batches",
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_usage_flush_batches_received_at", "usage_flush_batches", ["received_at"])


def downgrade() -> None:
    op.drop_table("usage_flush_batches")
    op.drop_table("value_usage_minutes")
    op.drop_table("llm_usage_minutes")
