"""Drop unused unscoped indexes ``ix_memories_status`` + ``ix_memories_visibility``.

CAURA-629 (2026-04-29). Both indexes were declared global (no
``tenant_id`` prefix) but every query in the codebase that filters by
``status`` or ``visibility`` already pre-narrows by ``tenant_id`` —
the planner uses ``ix_memories_tenant_id`` (or one of the
``tenant_id``-prefixed composites in ``__table_args__``) to bound the
scan to a single tenant's ~100-1000 rows, then applies status /
visibility as a cheap post-filter. The unscoped indexes provide no
read benefit on any tenant-scoped query path; they only add per-row
maintenance cost on every Memory INSERT/UPDATE.

Surfaced in the CAURA-627 deep-dive (audit_log + memory write
contention investigation, 2026-04-29) — flagged as low-risk hygiene
the planner would already prefer to ignore.

Migration semantics:
- ``DROP INDEX CONCURRENTLY`` so AlloyDB primary keeps serving
  writes through the operation. Non-blocking on AccessShare; only
  takes a brief AccessExclusive flash on metadata commit.
- ``IF EXISTS`` is belt-and-suspenders for the rare case where a
  staging-only or test-only environment never had these indexes
  (pre-001-bootstrap clones).
- The downgrade recreates them ``CONCURRENTLY`` for symmetry, even
  though they're not load-bearing — keeps the migration round-trip
  reversible without a CONCURRENTLY-constraint surprise on rollback.

Revision ID: 008
Revises: 007
Create Date: 2026-04-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``DROP INDEX CONCURRENTLY`` cannot run inside a transaction —
    # alembic's autocommit_block opens its own connection scope.
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_status")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_visibility")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_status ON memories (status)")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_visibility ON memories (visibility)")
