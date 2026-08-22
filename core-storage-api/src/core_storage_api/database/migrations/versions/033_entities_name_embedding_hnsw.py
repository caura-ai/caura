"""Add HNSW index on ``entities.name_embedding`` for cross-link discovery.

The entity cross-link discovery path
(``PostgresService.entity_discover_cross_links`` →
``POST /api/v1/storage/entities/discover-cross-links``) runs a per-candidate
approximate-nearest-neighbour search over a tenant's entities:

    JOIN LATERAL (
        SELECT ... FROM entities e
        WHERE e.tenant_id = :tenant_id AND e.name_embedding IS NOT NULL ...
        ORDER BY e.name_embedding <=> m.embedding
        LIMIT 10
    ) e ON true

``entities.name_embedding`` has had NO ANN index since 001 (migration 012
notes this explicitly). Without one, every candidate memory triggers a full
sequential scan of the tenant's entities — O(candidates x entities) cosine
computations in a single request. On large tenants this exceeds the caller's
120 s timeout: observed as a ``504 Gateway Timeout`` on the storage endpoint
(prod + staging, 2026-07). This mirrors ``ix_memories_embedding_hnsw`` (001)
so each lookup is O(log n), and speeds up entity linking generally.

CONCURRENTLY: a plain ``CREATE INDEX`` takes an AccessExclusiveLock that
blocks all writes to ``entities`` (including the extraction/upsert path) for
the duration of the build. Concurrent build matches the vector-index idiom in
001 / 012 and the general index pattern in 005 / 007 / 011 / 016 / 017 / 024.
``CONCURRENTLY`` cannot run inside a transaction, hence ``autocommit_block``.
``IF NOT EXISTS`` keeps it idempotent across the advisory-lock-serialised
startup migration path. ``m`` / ``ef_construction`` match the memories index.

Revision ID: 033
Revises: 032
Create Date: 2026-07-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "033"
down_revision: str | None = "032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # Keep ``CREATE INDEX CONCURRENTLY ... ix_... ON entities`` on one source
        # line so the ``test_no_plain_create_index_on_large_tables`` guard (which
        # regex-scans the source) actually validates the CONCURRENTLY clause on
        # this large-table index rather than silently skipping a split string.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_entities_name_embedding_hnsw ON entities "
            "USING hnsw (name_embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_entities_name_embedding_hnsw")
