"""Value metering: agent sessions, where each memory came from, and its reuse.

docs/plans/value-metering.md. Three tables, all keyed by tenant:

``agent_sessions`` — one row per agent session Caura has seen (MCP issues the
id; REST and SDKs send ``X-Caura-Session-Id``; the broker has its own). First
and last seen, and the calls it made to Caura.

``memory_origins`` — the work behind a memory, recorded once after it is
written: how long its session had worked since starting or since its previous
write (``origin_seconds``), the calls to Caura in that stretch (a lower bound
on turns), and, when a hook, plugin or SDK read them from the provider, the
tokens and turns spent. Never a model's own estimate.

``memory_reuse`` — one row per (session, memory) the first time a session
recalls a memory written in another session. The primary key is the "once per
session" rule, so ten recalls in one session stay one reuse.

Counts and ids only, never content. ``memory_origins`` and ``memory_reuse``
cascade with the memory row; ``agent_sessions`` goes with the tenant purge.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    #: ``mcp``, ``rest`` or ``broker``.
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    calls: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )

    __table_args__ = (
        CheckConstraint("calls >= 0", name="ck_agent_sessions_calls_nonneg"),
        Index("ix_agent_sessions_tenant_first_seen", "tenant_id", "first_seen"),
    )


class MemoryOrigin(Base):
    __tablename__ = "memory_origins"

    memory_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    written_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    #: Since the session started, or since its previous write.
    origin_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Calls to Caura in that stretch: a lower bound on the session's turns.
    origin_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Reported by a hook, plugin or SDK from the provider's own usage; NULL when none was.
    origin_tokens_in: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    origin_tokens_out: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    origin_turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Who reported the tokens: ``hook``, ``plugin`` or ``sdk``.
    origin_source: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "coalesce(origin_seconds, 0) >= 0 AND coalesce(origin_calls, 0) >= 0 "
            "AND coalesce(origin_tokens_in, 0) >= 0 AND coalesce(origin_tokens_out, 0) >= 0 "
            "AND coalesce(origin_turns, 0) >= 0",
            name="ck_memory_origins_nonneg",
        ),
        Index(
            "ix_memory_origins_tenant_session_written",
            "tenant_id",
            "session_id",
            "written_at",
        ),
    )


class MemoryReuse(Base):
    __tablename__ = "memory_reuse"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[str] = mapped_column(Text, primary_key=True)
    memory_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        primary_key=True,
    )
    reader_agent_id: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    writer_agent_id: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    writer_session_id: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    first_recalled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_memory_reuse_tenant_recalled", "tenant_id", "first_recalled_at"),
        # The cascade from ``memories`` finds a deleted memory's reuse rows by
        # this, instead of scanning the table once per deleted row (035).
        Index("ix_memory_reuse_memory", "memory_id"),
    )
