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
        # "what has this agent written" — the query ``agent_id`` exists to
        # serve. Mirrors ``ix_memories_tenant_agent``.
        Index("ix_documents_tenant_agent", "tenant_id", "agent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    fleet_id: Mapped[str | None] = mapped_column(Text)
    collection: Mapped[str] = mapped_column(Text, nullable=False)
    doc_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Who wrote this version (ax-0917-m-14). Nullable because every row
    # predating the column was written when there was no author to record,
    # and NULL says "we do not know" rather than naming someone who did not
    # write it. ``memories.agent_id`` is NOT NULL because a memory has never
    # been writable without one.
    agent_id: Mapped[str | None] = mapped_column(Text)
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
