"""``projection_models`` and ``memory_projections``: saved field layouts.

core-api laid a tenant's field out again in every process on every cold read,
paging every embedding to do it. A fit is now saved once per tenant, with each
memory's position under that fit's version; ``common/models/memory_projection.py``
has the contract. Numbers only. ``memory_projections.memory_id`` references
``memories`` ON DELETE CASCADE.

Two new, empty tables in one transaction: no lock on ``memories`` beyond the
FK's brief SHARE ROW EXCLUSIVE, and no CONCURRENTLY needed for their own index.

Revision ID: 078
Revises: 077
Create Date: 2026-11-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "078"
down_revision: str | None = "077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projection_models",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("model_version", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("dim", sa.Integer(), nullable=True),
        sa.Column("n_fit", sa.Integer(), nullable=True),
        sa.Column("params", sa.LargeBinary(), nullable=True),
        sa.Column("fitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.Text(), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "memory_projections",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column(
            "memory_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("model_version", sa.Text(), primary_key=True),
        sa.Column("x", sa.REAL(), nullable=False),
        sa.Column("y", sa.REAL(), nullable=False),
        sa.Column("cluster", sa.SmallInteger(), nullable=False),
        sa.Column("placed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("cluster >= 0", name="ck_memory_projections_cluster"),
    )
    # Backs the cascade from memories and the delete on a new embedding.
    op.create_index("ix_memory_projections_memory", "memory_projections", ["memory_id"])


def downgrade() -> None:
    op.drop_table("memory_projections")
    op.drop_table("projection_models")
