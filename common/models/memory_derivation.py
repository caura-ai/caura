"""MemoryDerivation — lineage for inferred memories (A55).

Records which upstream memory produced an inferred (``memories.is_inferred=true``)
memory. A memory may derive from several sources (one row each). This enables
**revalidation**: if an upstream source is later corrected or deleted, the
inferred memory (and any conflict it drove) can be re-checked or retracted.
See benchmark/A55-schema-design.md.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class MemoryDerivation(Base):
    __tablename__ = "memory_derivations"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    # The inferred memory …
    memory_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
    )
    # … derived from this upstream memory.
    source_memory_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "memory_id", "source_memory_id", name="uq_memory_derivations_edge"
        ),
        Index("ix_memory_derivations_memory", "memory_id"),
        Index("ix_memory_derivations_source", "source_memory_id"),
        # Declared by name rather than via ``index=True`` on the column, so the
        # name here and the name migration 049 creates are the same string in
        # both places. As ``index=True`` it was ``ix_memory_derivations_tenant_id``
        # in the model and absent from every migration (09/02 L-14) — while the
        # org purge deletes from this table by ``tenant_id`` and had no index to
        # use.
        Index("ix_memory_derivations_tenant_id", "tenant_id"),
    )
