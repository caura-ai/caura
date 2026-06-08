"""Restore unscoped indexes ``ix_memories_status`` + ``ix_memories_visibility``.

CAURA-632 (2026-04-29). Migration 008 dropped both indexes on the
premise that "every query already pre-narrows by ``tenant_id`` so the
unscoped indexes give no read benefit." That premise held in
*steady-state* — ``pg_stat_user_indexes`` showed zero scans, and the
planner does prefer the ``tenant_id``-prefixed composites for normal
single-tenant queries.

It DID NOT hold under the noisy-neighbor write storm shape that the
loadtest harness exercises. Bisect of regression 8.15x → 14.49x on
``noisy-neighbor-write`` between baseline ``1777411213`` and
``1777463809`` (after kill-switching CAURA-628, ruling that out)
identified two contributors that compound multiplicatively:

  1. The dropped indexes (this migration). Sampler queries in
     ``postgres_service.py`` (semantic-search candidate fetch,
     contradiction detection, statistics) issue
     ``WHERE status IN (...) AND visibility != 'scope_agent'``-style
     filters on a per-tenant scoped slice. Without the unscoped
     indexes, planner falls back to a tenant-id-only index scan +
     post-filter. Cheap in steady state, but under tenant-A storm of
     100 concurrent writes the per-tenant slice becomes hot and the
     post-filter rows contend with INSERT row-locks. Restoring the
     indexes lets the planner use bitmap-AND of two narrow indexes
     and skip the contended rows entirely.
  2. ``EMBED_ON_HOT_PATH=false`` + ``ENRICH_ON_HOT_PATH=false`` add
     ~5x write-side amplification. Independent of this migration —
     tracked as the second half of CAURA-632's bisect.

Migration semantics are mirror-image of 008:
- ``CREATE INDEX CONCURRENTLY`` so AlloyDB primary keeps serving
  writes through the build. Long-running on a multi-million-row
  ``memories`` table; no AccessExclusive flash, just the brief
  metadata-commit lock at the end.
- ``IF NOT EXISTS`` covers re-runs / rollback round-trips.
- The downgrade re-applies 008's drop ``CONCURRENTLY``, keeping the
  round-trip reversible.

Trade-off note: restoring the indexes re-adds the per-row maintenance
cost on every Memory INSERT/UPDATE that 008 was trying to remove. On
the noisy-neighbor metric the contention saved by index-scans is
~6x the cost of the maintenance writes, so it's a clear net win.
A future redesign could add tenant-scoped composites
(``(tenant_id, status)`` etc) to capture both wins, but that's a
larger schema change tracked separately.

Revision ID: 009
Revises: 008
Create Date: 2026-04-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``CREATE INDEX CONCURRENTLY`` cannot run inside a transaction —
    # alembic's autocommit_block opens its own connection scope.
    with op.get_context().autocommit_block():
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_status ON memories (status)")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_visibility ON memories (visibility)")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_status")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_visibility")
