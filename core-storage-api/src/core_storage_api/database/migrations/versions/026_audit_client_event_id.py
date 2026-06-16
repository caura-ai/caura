"""Per-event idempotency on audit_log: ``client_event_id`` + partial unique.

Makes the audit bulk flush (``POST /audit-logs/bulk``) safely retryable on
transient storage errors (ReadTimeout / 5xx) without double-appending to the
tamper-evident hash chain. core-api mints a UUID ``client_event_id`` per event
in ``log_action``; ``PostgresService._audit_chain_one_tenant`` dedups
already-chained events under the per-tenant ``audit_chain_head`` lock — BEFORE
assigning ``seq`` — so a retry of a lost-ack batch re-sends the same ids, the
already-committed events are skipped, and the survivors get contiguous seqs (no
gap, head stays consistent).

The partial unique index ``(tenant_id, client_event_id) WHERE client_event_id
IS NOT NULL`` is the DB-level backstop. NULL values are excluded so the legacy
single-event path (``POST /audit-logs``) and pre-026 rows are unaffected.
``CREATE UNIQUE INDEX CONCURRENTLY`` keeps the build off the audit_log write
lock — required on the large append-only ``audit_log`` at any non-trivial row
count. (This deliberately follows migration 007's online pattern and NOT
migration 025, whose plain in-transaction ``CREATE INDEX`` on ``audit_log``
stalled storage-writer startup past the 300s migration-lock timeout.)

Revision ID: 026
Revises: 025
Create Date: 2026-06-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "026"
down_revision: str | None = "025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_audit_log_client_event_id_unique"


def upgrade() -> None:
    # ``ADD COLUMN IF NOT EXISTS`` (not op.add_column) keeps upgrade() retry-safe:
    # entering autocommit_block() below COMMITS this column add, so if the
    # CONCURRENTLY index build then fails (timeout / operator cancel), the column
    # persists but alembic_version is NOT stamped — a plain ADD COLUMN would raise
    # "column already exists" on the retry and wedge the deploy. IF NOT EXISTS makes
    # the add idempotent. Metadata-only on a clean run (no row rewrite, just the
    # AccessExclusive flash to update pg_class). The index build is the online part.
    op.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS client_event_id text")
    # CREATE INDEX CONCURRENTLY cannot run inside a transaction. Mirrors 007:
    # clean up an interrupted prior build (``indisvalid = false``) so the
    # rebuild completes instead of silently skipping via IF NOT EXISTS and
    # leaving a useless invalid index behind.
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
            ON audit_log (tenant_id, client_event_id)
            WHERE client_event_id IS NOT NULL
            """
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
    op.drop_column("audit_log", "client_event_id")
