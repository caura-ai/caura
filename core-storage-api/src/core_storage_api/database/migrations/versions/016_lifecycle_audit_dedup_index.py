"""Add indexes supporting the CAURA-657 dedup gate and purge fanout
discovery query.

1. Partial index on ``lifecycle_audit (org_id, action, finished_at
   DESC) WHERE status = 'success'`` — backs
   ``lifecycle_audit_has_recent_success``. The existing index from 015
   covers ``(org_id, action, started_at DESC)``, wrong ordering and
   ignores ``status``, so the planner post-filters every row for that
   ``(org_id, action)`` pair.

2. Partial index on ``memories (tenant_id) WHERE deleted_at IS NOT
   NULL`` — backs the bounded purge-fanout discovery query in
   ``list_tenants_with_purgeable_memories``. Existing partial indexes
   on ``memories`` are all keyed on ``deleted_at IS NULL`` (active
   path); without a complementary index on the soft-deleted path the
   discovery query falls back to a full table scan.

Both indexes are built ``CONCURRENTLY`` to avoid the
AccessExclusiveLock the plain ``CREATE INDEX`` takes — matches the
pattern in 005/007/011.

Revision ID: 016
Revises: 015
Create Date: 2026-05-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "016"
down_revision: str | None = "015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "idx_lifecycle_audit_dedup_gate "
            "ON lifecycle_audit (org_id, action, finished_at DESC) "
            "WHERE status = 'success'"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_memories_purgeable "
            "ON memories (tenant_id) "
            "WHERE deleted_at IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_purgeable")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_lifecycle_audit_dedup_gate")
