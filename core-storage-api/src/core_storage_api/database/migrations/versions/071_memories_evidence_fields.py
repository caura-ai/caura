"""``memories`` evidence fields: where a memory came from and whether anyone checked it.

Evidence contract v1 (``docs/plans/evidence-contract-v1.md``), EC-07. Six
columns:

- ``observed_at``: when the fact was observed, as the writer reports it. NULL
  when the writer did not say; ``created_at`` stays the write time.
- ``trace_id``: the session or request the write came from. A client may send
  one; otherwise core-api stamps the MCP session id, the REST
  ``X-Caura-Session-Id``, or a per-request id. Distinct from ``run_id``, which
  stays whatever the client sends.
- ``tool_call_id``: the tool call whose result the memory records.
- ``verification_status``: server-owned. ``unverified`` (the default),
  ``tool_observed``, ``corroborated`` (reserved), ``human_verified``,
  ``disputed``. A CHECK holds the column to those five.
- ``verified_by`` / ``verified_at``: who moved it off ``unverified``, and when.

LOCK SAFETY. ``memories`` is large. The five nullable columns rewrite nothing.
``verification_status`` is NOT NULL with a constant default, which PG11+ stores
in the catalog rather than writing to every row, so it is O(1) as well; the
embeddings are not touched. The CHECK is added ``NOT VALID`` (O(1) under the
column adds' lock) and validated in its own transaction, which scans under
ShareUpdateExclusive and blocks neither reads nor writes: 038's pattern.

``ix_memories_verification`` backs the planned ``verified_only`` recall filter.
Partial on ``verification_status <> 'unverified'``, so it holds only rows
someone or something checked: empty when this runs, and a write of an ordinary
memory never enters it. Built CONCURRENTLY, last, after clearing a half-built
one from an interrupted run (065).

The column adds use ``if_not_exists`` and the CHECK is dropped before it is
added: the autocommit block commits them before the version row moves, so a
retry runs ``upgrade()`` from the top over columns that already exist.

Revision ID: 071
Revises: 070
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from core_storage_api.database.migration_helpers import drop_invalid_indexes

revision: str = "071"
down_revision: str | None = "070"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_memories_verification_status"
# Frozen here rather than imported: a migration describes the schema as of its
# revision, and ``common.models.memory.VERIFICATION_STATUSES`` may grow later.
_ALLOWED = "'unverified', 'tool_observed', 'corroborated', 'human_verified', 'disputed'"


def upgrade() -> None:
    for column in (
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column("tool_call_id", sa.Text(), nullable=True),
        sa.Column("verification_status", sa.Text(), nullable=False, server_default=sa.text("'unverified'")),
        sa.Column("verified_by", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    ):
        op.add_column("memories", column, if_not_exists=True)
    # Postgres has no ADD CONSTRAINT IF NOT EXISTS; see the retry note above.
    op.execute(f"ALTER TABLE memories DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE memories ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK (verification_status IN ({_ALLOWED})) NOT VALID"
    )

    with op.get_context().autocommit_block():
        op.execute(f"ALTER TABLE memories VALIDATE CONSTRAINT {_CONSTRAINT}")
        drop_invalid_indexes("ix_memories_verification")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_verification ON memories "
            "(tenant_id, verification_status) "
            "WHERE verification_status <> 'unverified'"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_verification")
    op.execute(f"ALTER TABLE memories DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    for column in (
        "verified_at",
        "verified_by",
        "verification_status",
        "tool_call_id",
        "trace_id",
        "observed_at",
    ):
        op.drop_column("memories", column)
