"""Saved field layouts: a tenant's fitted model and its memories' positions.

core-api's field layout (``core_api.services.memory_field``) is a two-level
PCA fit over a tenant's embeddings. Computing it pages every embedding, so it
is done once per tenant and saved here, and readers serve the saved positions:

* ``projection_models`` holds the latest fit per tenant: an opaque parameter
  blob whose format core-api owns (what it needs to place a new embedding
  without refitting), the version naming that fit, and the claim one process
  holds while it fits. Numbers only, never memory text.
* ``memory_projections`` holds each memory's position (x, y, cluster) under a
  model version. Keyed by version as well as memory, so a refit can write the
  new version's positions while readers still place memories under the old
  one; saving the new model deletes the old version's rows, and a position is
  accepted only under the saved version or the live claim's, so none is left
  behind under a retired one. ``memory_id`` references ``memories`` ON DELETE
  CASCADE. Storage deletes a memory's rows when its embedding changes, and
  refuses a position computed from the row before it changed, so a position
  never outlives the vector it was computed from.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    REAL,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class ProjectionModel(Base):
    """A tenant's latest field-layout fit, and the claim on the next one.

    The row exists from a tenant's first claim; its fit columns stay NULL until
    a model is saved. A claim is ``claim_token`` with ``claim_expires_at``: a
    process may take it when there is none, when it has expired, or when the
    token is its own (a retried request). Only the holder of a live claim may
    save a model, and saving clears the claim.
    """

    __tablename__ = "projection_models"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    model_version: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str | None] = mapped_column(Text)
    dim: Mapped[int | None] = mapped_column(Integer)
    n_fit: Mapped[int | None] = mapped_column(Integer)
    params: Mapped[bytes | None] = mapped_column(LargeBinary)
    fitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(Text)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryProjection(Base):
    """One memory's position on the field under one model version."""

    __tablename__ = "memory_projections"

    # The key leads with the tenant for the purge's per-tenant delete; the
    # field page joins on all three columns.
    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    memory_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model_version: Mapped[str] = mapped_column(Text, primary_key=True)
    x: Mapped[float] = mapped_column(REAL, nullable=False)
    y: Mapped[float] = mapped_column(REAL, nullable=False)
    cluster: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        CheckConstraint("cluster >= 0", name="ck_memory_projections_cluster"),
        # Backs the cascade from memories and the delete on a new embedding.
        Index("ix_memory_projections_memory", "memory_id"),
    )
