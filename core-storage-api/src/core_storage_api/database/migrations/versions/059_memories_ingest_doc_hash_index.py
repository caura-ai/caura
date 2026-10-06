"""Partial index serving the ingest doc-hash lookup.

L-193. Every /ingest/preview and /ingest/file looks its document's hash up
(``find_prior_ingest_by_doc_hash``) before any LLM work, and nothing indexed
``metadata ->> 'doc_hash'``. On a cache miss, the common case for a new
document, each preview filtered every live row of the tenant on the primary, so
its cost grew with the tenant rather than with the document.

PARTIAL on live ingest rows, keyed on ``(tenant_id, metadata ->> 'doc_hash')``.
A doc hash is a content hash, so the key alone narrows the lookup to that
document's rows, and the agent and fleet filters apply to those few. The key
expression and predicate must match ``postgres_service.prior_ingest_where`` for
the planner to use it.

Write cost: an ingested fact enters the index at insert and leaves it on delete.
Other rows never enter it.

Revision ID: 059
Revises: 058
Create Date: 2026-10-05
"""

from collections.abc import Sequence

from alembic import op

from core_storage_api.database.migration_helpers import drop_invalid_indexes

revision: str = "059"
down_revision: str | None = "058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CONCURRENTLY in an autocommit block: ``memories`` is a large table.
    with op.get_context().autocommit_block():
        drop_invalid_indexes("ix_memories_ingest_doc_hash")
        # One source line for the ``CREATE INDEX ... ON <table>`` prefix, so the
        # CONCURRENTLY guard's regex sees it.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_ingest_doc_hash ON memories "
            "(tenant_id, (metadata ->> 'doc_hash')) "
            "WHERE deleted_at IS NULL AND (metadata ->> 'source') = 'ingest'"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_ingest_doc_hash")
