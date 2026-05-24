"""Create ``dedup_reviews`` queue table (A1 #18).

One row per ambiguous dedup decision surfaced by
``CheckSemanticDuplicate`` (A1 #16/#17). Rows are enqueued in three
shapes:

  - ``auto_reject``  — sim ≥ AUTO_THRESHOLD (0.97); the write was
                        409'd before any LLM call. User may want to
                        override ("no, this is a different memory").
  - ``judge_band_reject`` — JUDGE ≤ sim < AUTO; LLM judge said
                            ``is_duplicate=True`` at confidence ≥
                            ``DEDUP_JUDGE_CONFIDENCE_THRESHOLD``
                            (0.60); the write was 409'd.
  - ``judge_low_conf_accept`` — JUDGE ≤ sim < AUTO; judge said
                                ``is_duplicate=True`` but at
                                confidence below threshold. The write
                                was ACCEPTED but the near-miss is
                                worth a human look.

``new_memory_id`` is nullable because rejected writes never persist
a row — only the content snapshot exists.

Tenant scoping mirrors ``lifecycle_audit``: plain text, no FK to a
tenants table, so OSS and enterprise deployments key consistently.

Revision ID: 018
Revises: 017
Create Date: 2026-05-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "018"
down_revision: str | None = "017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dedup_reviews",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("fleet_id", sa.Text(), nullable=True),
        sa.Column("agent_id", sa.Text(), nullable=False),
        # nullable: rejected writes never persisted a row, so this is
        # NULL when ``decision_band IN ('auto_reject',
        # 'judge_band_reject')`` and the user didn't subsequently
        # override.
        sa.Column("new_memory_id", PG_UUID(as_uuid=True), nullable=True),
        sa.Column("candidate_memory_id", PG_UUID(as_uuid=True), nullable=False),
        # Content snapshots — preserved even if the underlying memories
        # are later deleted, so the review UI can always show the user
        # what was compared at decision time.
        sa.Column("new_content", sa.Text(), nullable=False),
        sa.Column("candidate_content", sa.Text(), nullable=False),
        sa.Column("similarity", sa.Float(), nullable=False),
        sa.Column("judge_verdict", sa.Boolean(), nullable=True),
        sa.Column("judge_confidence", sa.Float(), nullable=True),
        sa.Column("decision_band", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Primary list query: pending reviews for a tenant, newest first.
    op.execute(
        "CREATE INDEX idx_dedup_reviews_tenant_status_created "
        "ON dedup_reviews (tenant_id, status, created_at DESC)"
    )


def downgrade() -> None:
    op.drop_table("dedup_reviews")
