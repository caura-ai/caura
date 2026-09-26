"""Recall logging models — ``recall_event`` + ``recall_candidate``.

A per-tenant, opt-in diagnostic log of agent-chosen ``caura_recall`` calls:
what was queried, with what scope, and which memories were considered (raw
cosine + final score), including a few near-misses below the similarity floor.
Used to answer "why aren't good memories recalled?" — see migration 027.

Candidates store only ``memory_id`` (a pointer); content is JOINed from
``memories`` on demand, never duplicated here.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class RecallEvent(Base):
    __tablename__ = "recall_event"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    agent_id: Mapped[str | None] = mapped_column(Text)
    # 'mcp_recall' today (the agent-chosen tool). The plugin's automatic
    # ``/search`` is intentionally not logged.
    source: Mapped[str] = mapped_column(Text, nullable=False)
    query_text: Mapped[str | None] = mapped_column(Text)
    strategy: Mapped[str | None] = mapped_column(Text)
    filter_agent_id: Mapped[str | None] = mapped_column(Text)
    fleet_scope: Mapped[str | None] = mapped_column(Text)
    top_k: Mapped[int | None] = mapped_column(Integer)
    min_similarity: Mapped[float | None] = mapped_column(Float)
    result_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    top_score: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    # Named to match migration 027, which is what actually exists. As
    # ``index=True`` on ``tenant_id`` this was ``ix_recall_event_tenant_id`` —
    # a single-column index the migration never created, while the composite
    # below (which it did create, and which serves the same tenant-prefixed
    # lookups) went undeclared. 09/02 L-14, both directions in one table.
    __table_args__ = (Index("ix_recall_event_tenant_ts", "tenant_id", "ts"),)


class RecallCandidate(Base):
    __tablename__ = "recall_candidate"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    recall_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("recall_event.id", ondelete="CASCADE"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    # Pointer only — JOIN to ``memories`` for content. Not a FK so a later
    # memory purge can't delete the diagnostic record.
    memory_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    vec_sim: Mapped[float | None] = mapped_column(Float)
    final_score: Mapped[float | None] = mapped_column(Float)
    recall_boost: Mapped[float | None] = mapped_column(Float)
    returned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # Migration 027's names again — ``index=True`` produced
    # ``ix_recall_candidate_recall_event_id`` / ``ix_recall_candidate_memory_id``,
    # neither of which exists anywhere.
    __table_args__ = (
        Index("ix_recall_candidate_event", "recall_event_id"),
        Index("ix_recall_candidate_memory", "memory_id"),
    )
