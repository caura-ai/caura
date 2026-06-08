"""Per-attempt idempotency on memories: ``client_request_id`` + partial unique.

Bulk write atomicity (CAURA-602): every bulk row carries a server-derived
``client_request_id = f"{X-Bulk-Attempt-Id}:{index}"`` (or NULL for
non-bulk paths). The partial unique index converts a retry of the same
logical batch into deterministic per-row resolution: rows already
committed by a prior attempt are recognised via ``ON CONFLICT`` instead
of being silently re-inserted, returning duplicate-attempt status with
the canonical id. Eliminates the silent-create class observed in
loadtest-1777301515 where committed rows ack'd as 0 because the response
to the client was lost.

The constraint scope mirrors content-hash dedup (``tenant_id``,
``fleet_id``, ``client_request_id``). ``fleet_id`` is wrapped in
``COALESCE(fleet_id, '')`` because PostgreSQL treats NULLs as distinct
in unique indexes — without the COALESCE, two retries of a fleetless
attempt would both pass the conflict check and silently double-insert,
exactly the regression this migration is meant to prevent. NULL
``client_request_id`` values are excluded so legacy single-write callers
and pre-rollout rows are unaffected. ``CREATE UNIQUE INDEX CONCURRENTLY``
keeps the build off the write lock — required on AlloyDB primary at any
non-trivial row count.

Revision ID: 007
Revises: 006
Create Date: 2026-04-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_memories_attempt_unique"


def upgrade() -> None:
    # ``ALTER TABLE ADD COLUMN`` with no DEFAULT is metadata-only on
    # PostgreSQL — no row rewrite, no full-table lock beyond the
    # AccessExclusive flash needed to update pg_class.
    op.add_column(
        "memories",
        sa.Column("client_request_id", sa.Text(), nullable=True),
    )
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction.
    # Mirrors the pattern in 005 — clean up an interrupted prior build
    # (``indisvalid = false``) so the rebuild completes instead of
    # silently skipping via IF NOT EXISTS and leaving a useless index.
    with op.get_context().autocommit_block():
        connection = op.get_context().connection
        if connection is None:
            raise RuntimeError("online migration requires a connection")
        result = connection.execute(
            sa.text(
                """
                SELECT 1 FROM pg_index i
                JOIN pg_class c ON c.oid = i.indexrelid
                WHERE c.relname = :name
                  AND NOT i.indisvalid
                """
            ),
            {"name": _INDEX_NAME},
        )
        if result.fetchone():
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
        op.execute(
            f"""
            CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS
                {_INDEX_NAME}
            ON memories (tenant_id, COALESCE(fleet_id, ''), client_request_id)
            WHERE deleted_at IS NULL AND client_request_id IS NOT NULL
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
    op.drop_column("memories", "client_request_id")
