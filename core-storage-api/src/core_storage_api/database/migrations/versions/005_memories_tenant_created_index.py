"""Composite partial index for cursor-paginated memory listing (CAURA-595).

Backs ``list_by_filters`` and the ``memclaw_list`` MCP tool. The
hot-path query is::

    SELECT ... FROM memories
    WHERE tenant_id = $1
      AND deleted_at IS NULL
      AND (created_at, id) < ($cursor_ts, $cursor_id)   -- when paginating
    ORDER BY created_at DESC, id DESC
    LIMIT $limit + 1

Pre-this-migration the planner satisfied the ``tenant_id`` predicate
via ``ix_memories_tenant_<*>`` (single-column on tenant_id, or any of
the tenant-prefixed composites that don't include ``created_at``)
and then sorted in memory. At ~5k rows / tenant from the load tests
the sort step dominated the response: bench_list p99 = 982ms vs the
800ms target.

The new index matches the query exactly:

* tenant_id is the equality predicate.
* (created_at DESC, id DESC) matches the ORDER BY exactly so the
  planner can read the index in order and stop at LIMIT.
* The partial ``WHERE deleted_at IS NULL`` keeps the index lean —
  soft-deleted rows are never read on the cursor path
  (``include_deleted=False`` is the default and only the explicit
  admin-style ``include_deleted=true`` reads them, which is rare).

CONCURRENTLY is required so the build doesn't take a write lock on
``memories``. Alembic's autocommit_block lifts the implicit
transaction so PostgreSQL accepts ``CREATE INDEX CONCURRENTLY``.

Revision ID: 005
Revises: 004
Create Date: 2026-04-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    with op.get_context().autocommit_block():
        # If a previous CONCURRENTLY build was interrupted, the index
        # exists in pg_index with ``indisvalid = false`` — it imposes
        # write overhead but isn't used by the planner. ``IF NOT
        # EXISTS`` below silently skips that case, leaving the broken
        # index in place. Detect with a SELECT and issue the drop as
        # its own top-level statement so the autocommit_block actually
        # applies — wrapping ``DROP INDEX CONCURRENTLY`` in a ``DO``
        # block doesn't, since PL/pgSQL anonymous blocks run inside an
        # implicit transaction.
        connection = op.get_context().connection
        if connection is None:
            raise RuntimeError("online migration requires a connection")
        result = connection.execute(
            sa.text(
                """
                SELECT 1 FROM pg_index i
                JOIN pg_class c ON c.oid = i.indexrelid
                WHERE c.relname = 'ix_memories_tenant_created_active'
                  AND NOT i.indisvalid
                """
            )
        )
        if result.fetchone():
            # ``IF EXISTS`` so a race against a concurrent manual drop
            # (or a second migration runner) doesn't abort the migration
            # — the SELECT and DROP are separate autocommit statements
            # with no lock between them.
            op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_tenant_created_active")
        op.execute(
            """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
                ix_memories_tenant_created_active
            ON memories (tenant_id, created_at DESC, id DESC)
            WHERE deleted_at IS NULL
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_tenant_created_active")
