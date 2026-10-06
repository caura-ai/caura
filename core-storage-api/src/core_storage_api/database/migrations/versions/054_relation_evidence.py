"""Record each memory that asserted a relation.

The existing single evidence pointer stores only the latest assertion. Seed
the new table with that known assertion; older overwritten assertions cannot
be reconstructed safely from endpoint links alone.

Revision ID: 054
Revises: 053
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "054"
down_revision: str | None = "053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "relation_evidence",
        sa.Column(
            "relation_id",
            sa.Uuid(),
            sa.ForeignKey("relations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "memory_id",
            sa.Uuid(),
            sa.ForeignKey("memories.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index("ix_relation_evidence_memory_id", "relation_evidence", ["memory_id"])
    op.execute(
        "INSERT INTO relation_evidence (relation_id, memory_id) "
        "SELECT r.id, r.evidence_memory_id FROM relations r "
        "JOIN memories m ON m.id = r.evidence_memory_id AND m.tenant_id = r.tenant_id "
        "WHERE r.evidence_memory_id IS NOT NULL AND m.deleted_at IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_relation_evidence_memory_id", table_name="relation_evidence")
    op.drop_table("relation_evidence")
