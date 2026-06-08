import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from common.constants import VECTOR_DIM
from common.models.base import Base


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "collection",
            "doc_id",
            name="uq_documents_tenant_collection_doc",
        ),
        Index("ix_documents_tenant_collection", "tenant_id", "collection"),
        Index("ix_documents_data", "data", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    fleet_id: Mapped[str | None] = mapped_column(Text)
    collection: Mapped[str] = mapped_column(Text, nullable=False)
    doc_id: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Optional embedding populated when op=write resolves a string to embed
    # from data["summary"] (or data["description"] for the skills collection
    # back-compat path). NULL = not indexed for semantic search.
    embedding = mapped_column(Vector(VECTOR_DIM), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
