"""Agent activity digest: cached per-agent LLM summaries.

Backs the precomputed "what did this agent do this day/week" report
(GET /api/v1/reports/agent-activity). Rows are grouped by ``run_id`` — one
scheduled pass (default nightly, core-operations) writes one row per agent.

Distinct from ``analysis_reports`` (the crystallization report): that stores a
single JSONB blob per run; this stores one queryable row per agent so the UI
can render/deep-link a per-agent narrative and the read path can filter by
``agent_id``. The UNIQUE constraint makes re-runs of the same window idempotent
(upsert), and the ``(tenant_id, period, window_start DESC)`` index serves the
"latest run" hot read.

No DB CHECK on ``period``/``status``: the value sets are validated at the app
layer, mirroring ``memories.visibility`` and ``agents.belonging_type``.

Revision ID: 029
Revises: 028
Create Date: 2026-07-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "029"
down_revision: str | None = "028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_activity_digests",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("fleet_id", sa.Text(), nullable=True),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("period", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("narrative", sa.Text(), nullable=True),
        sa.Column("sections", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("source_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("recall_count", sa.Integer(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_agent_activity_digests_run_id", "agent_activity_digests", ["run_id"])
    op.create_index("ix_agent_activity_digests_tenant_id", "agent_activity_digests", ["tenant_id"])
    op.create_index(
        "ix_agent_digest_latest",
        "agent_activity_digests",
        ["tenant_id", "period", sa.text("window_start DESC")],
    )
    # Idempotency guard. Two PARTIAL unique indexes instead of a plain UNIQUE
    # constraint: Postgres treats NULLs as distinct, so UNIQUE(... fleet_id ...)
    # would let duplicate no-fleet digests through when fleet_id IS NULL.
    op.create_index(
        "uq_agent_digest_window_fleet",
        "agent_activity_digests",
        ["tenant_id", "fleet_id", "agent_id", "period", "window_start"],
        unique=True,
        postgresql_where=sa.text("fleet_id IS NOT NULL"),
    )
    op.create_index(
        "uq_agent_digest_window_no_fleet",
        "agent_activity_digests",
        ["tenant_id", "agent_id", "period", "window_start"],
        unique=True,
        postgresql_where=sa.text("fleet_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_agent_digest_window_no_fleet", table_name="agent_activity_digests")
    op.drop_index("uq_agent_digest_window_fleet", table_name="agent_activity_digests")
    op.drop_index("ix_agent_digest_latest", table_name="agent_activity_digests")
    op.drop_index("ix_agent_activity_digests_tenant_id", table_name="agent_activity_digests")
    op.drop_index("ix_agent_activity_digests_run_id", table_name="agent_activity_digests")
    op.drop_table("agent_activity_digests")
