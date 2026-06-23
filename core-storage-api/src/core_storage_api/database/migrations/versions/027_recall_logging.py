"""Recall logging: ``recall_event`` + ``recall_candidate``.

Opt-in, per-tenant diagnostic log answering "why aren't good memories
recalled?". One ``recall_event`` row per agent-chosen ``memclaw_recall`` call
(the plugin's automatic ``/search`` is NOT logged), plus a handful of
``recall_candidate`` rows — the returned top-k *and* a few near-misses just
below the similarity floor, each carrying the raw cosine + final score. The
candidate stores only ``memory_id`` (a pointer); content is JOINed from
``memories`` on demand, never duplicated here.

Both tables are RLS-scoped by ``tenant_id`` like every other tenant table and
are written fire-and-forget from the search pipeline (no request latency).
Logging is gated by the ``observability.recall_logging_enabled`` org setting
(default off), so existing tenants see zero new rows until they opt in.

Revision ID: 027
Revises: 026
Create Date: 2026-06-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "027"
down_revision: str | None = "026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recall_event",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("agent_id", sa.Text()),
        # 'mcp_recall' today (agent-chosen). Reserved for future sources.
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("query_text", sa.Text()),
        sa.Column("strategy", sa.Text()),
        sa.Column("filter_agent_id", sa.Text()),
        sa.Column("fleet_scope", sa.Text()),
        sa.Column("top_k", sa.Integer()),
        sa.Column("min_similarity", sa.Float()),
        sa.Column("result_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("top_score", sa.Float()),
        sa.Column("latency_ms", sa.Integer()),
    )
    # Drives the per-tenant, time-windowed reads (and the retention purge).
    op.create_index("ix_recall_event_tenant_ts", "recall_event", ["tenant_id", "ts"])

    op.create_table(
        "recall_candidate",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "recall_event_id",
            sa.Uuid(),
            sa.ForeignKey("recall_event.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        # Pointer only — content is JOINed from ``memories`` when needed.
        # Deliberately NOT a FK: a later memory purge must not delete the
        # diagnostic record of what recall returned.
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("vec_sim", sa.Float()),  # raw 0..1 cosine relevance
        sa.Column("final_score", sa.Float()),  # blended score after weight/boost
        sa.Column("recall_boost", sa.Float()),  # popularity multiplier actually applied
        # True = returned to the agent (passed floor + within top_k);
        # False = near-miss logged for diagnosis.
        sa.Column("returned", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_recall_candidate_event", "recall_candidate", ["recall_event_id"])
    # "which recalls ever returned memory X" + per-memory diagnosis joins.
    op.create_index("ix_recall_candidate_memory", "recall_candidate", ["memory_id"])


def downgrade() -> None:
    op.drop_table("recall_candidate")
    op.drop_table("recall_event")
