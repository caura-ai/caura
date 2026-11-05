"""``memory_exposure`` — which agents a memory was handed to, and how often.

Correction propagation (``docs/plans/correction-propagation.md``), D1. One row
per (tenant, memory, agent): the first and the latest time a read returned the
memory to that agent, how many reads did, and the trace of the latest one.
When a person corrects the memory, these rows are the agents to tell.

Always on, unlike ``recall_event`` (opt-in, 30-day retention): an agent that
read a memory a year ago still holds what it read. Written on every read that
returns memories to an agent (MCP recall, REST ``/search`` and ``/recall``,
get-by-id), one batched upsert per read, fire-and-forget. A chunk or an atomic
fact counts for its parent, the memory a person corrects. Person reads are not
recorded.

Ids and counts only, never content. ``memory_id`` references ``memories`` ON
DELETE CASCADE, so the rows live exactly as long as the memory.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class MemoryExposure(Base):
    __tablename__ = "memory_exposure"

    # The key is the upsert's conflict target and the "who saw memory X" read:
    # every lookup names the tenant and the memory.
    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    memory_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    agent_id: Mapped[str] = mapped_column(Text, primary_key=True)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
    #: The server trace (``session_context.server_trace_id``) of the latest read.
    last_trace_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint("count >= 1", name="ck_memory_exposure_count_positive"),
        # The cascade from ``memories`` finds a deleted memory's rows by this,
        # instead of scanning the table once per deleted row (035, 067).
        Index("ix_memory_exposure_memory", "memory_id"),
    )
