"""Partial indexes on ``memories.tenant_id`` and ``memories.agent_id``
to support the public ``COUNT(DISTINCT)`` queries that back ``/api/v1/stats``.

Backs the public landing-page tile queries:
  - ``COUNT(DISTINCT tenant_id) FROM memories WHERE deleted_at IS NULL``
  - ``COUNT(DISTINCT agent_id)  FROM memories WHERE deleted_at IS NULL
                                              AND agent_id IS NOT NULL``

Without dedicated partial indexes, both queries fall back to a full
heap scan of every live row on every landing-page hit. The existing
partial index ``ix_memories_tenant_created_active`` is on
``(tenant_id, created_at DESC, id DESC) WHERE deleted_at IS NULL`` —
PostgreSQL won't use it efficiently for a bare ``COUNT(DISTINCT
tenant_id)`` aggregate because the leading column ordering doesn't
let it deduplicate without sorting.

These indexes are deliberately narrow: ``tenant_id`` (or ``agent_id``)
alone, partial on ``deleted_at IS NULL``. They're maintained on every
write but only touched by these two specific aggregate queries on
read — typical write-amplification cost for a read-mostly counter
surface.

Revision ID: 011
Revises: 010
Create Date: 2026-05-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "011"
down_revision: str | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``CREATE INDEX CONCURRENTLY`` cannot run inside a transaction —
    # alembic's autocommit_block opens its own connection scope. Without
    # this, the index build holds a ShareLock on ``memories`` for the
    # duration of the build, blocking every write to the table — minutes
    # of write downtime on a large table. ``CONCURRENTLY`` builds in the
    # background without blocking writes (does take longer to complete).
    # ``IF NOT EXISTS`` makes the migration idempotent against a partial
    # prior run that already created one of the two indexes — matches
    # the pattern in 009_restore_unscoped_memory_indexes.py.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_tenant_id_active "
            "ON memories (tenant_id) WHERE deleted_at IS NULL"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_agent_id_active "
            "ON memories (agent_id) WHERE deleted_at IS NULL AND agent_id IS NOT NULL"
        )


def downgrade() -> None:
    # ``DROP INDEX CONCURRENTLY`` is also non-transactional and avoids
    # taking the access-exclusive lock that the plain DROP needs while
    # waiting for in-flight reads to complete. ``IF EXISTS`` guards the
    # downgrade against running on a DB where the upgrade was rolled
    # back partway through.
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_agent_id_active")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_tenant_id_active")
