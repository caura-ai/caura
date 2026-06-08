"""Add ``embedding`` column + HNSW index to ``documents``.

Opt-in semantic search for the doc store. Existing rows keep
``embedding = NULL`` and do not participate in ``memclaw_doc op=search``
— only rows written with ``embed_field=...`` get embedded. A partial
HNSW index (``WHERE embedding IS NOT NULL``) keeps the index small
while the feature rolls out.

Revision ID: 003
Revises: 002
Create Date: 2026-04-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Column is nullable: existing docs have no embedding until re-written
    # with embed_field. Dim matches VECTOR_DIM (768) used across the schema.
    op.add_column(
        "documents",
        sa.Column("embedding", Vector(768), nullable=True),
    )

    # Partial HNSW index — skips rows where embedding IS NULL. Same
    # operator + build params as the memories index (001_initial_schema).
    op.execute("""
        CREATE INDEX ix_documents_embedding_hnsw ON documents
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        WHERE embedding IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_documents_embedding_hnsw")
    op.drop_column("documents", "embedding")
