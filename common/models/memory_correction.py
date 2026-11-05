"""``memory_correction`` and its recipients — who to tell about a correction.

Correction propagation (``docs/plans/correction-propagation.md``), CP-10. One
``memory_correction`` row per ``caura.memory.corrected`` event: what changed,
who changed it, when, and the old and new summaries the event carried. Its
``memory_correction_recipient`` rows are the agents that read the memory
before the correction (``memory_exposure.first_seen < corrected_at``), the
correcting agent excluded, most recent readers first, at most
:data:`MAX_RECIPIENTS`; ``truncated`` counts the readers past the cap.

A correction starts as ``preview``: the person sees the list before anything is
sent. Sending (CP-11, migration 074) moves it to ``sending`` and, once no
recipient waits on the bus, ``sent``; ``closed`` when every recipient confirmed
or failed. Each recipient gets a ``channel`` (``bus``, or ``recall`` when the
bus cannot reach it) and its ``notice_state`` moves on from ``pending``.

``event_id`` is unique, so a redelivered event finds the record it already
wrote instead of writing a second. Both tables reference their parent ON
DELETE CASCADE: a hard-deleted memory takes its corrections with it.

``old_summary`` is the text that was wrong. Show it only to a person or to an
agent on the recipient list (``common.events.memory_corrected``).
"""

import uuid
from datetime import datetime

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base

#: Agents told about one correction (D4). The rest are counted in ``truncated``.
MAX_RECIPIENTS = 50

CORRECTION_KINDS = ("content_edit", "status", "unverify", "conflict_resolve")
CORRECTION_STATUSES = ("preview", "sending", "sent", "closed")
#: ``pending`` until sent; then the bus outcome (``sent``, ``delivered``,
#: ``confirmed``, ``late``, ``failed``). On the ``recall`` channel (an agent the
#: bus does not know, or no bus) it stays ``pending`` until a recall hands it
#: the notice (``delivered``), then ``confirmed``. ``unreachable`` is no longer
#: written; rows from before keep it and read like ``pending`` on recall.
NOTICE_STATES = ("pending", "sent", "delivered", "confirmed", "late", "failed", "unreachable")
NOTICE_CHANNELS = ("bus", "recall")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class MemoryCorrection(Base):
    __tablename__ = "memory_correction"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    memory_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The ``caura.memory.corrected`` envelope's ``event_id``: one record per event.
    event_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, unique=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``user``, ``agent`` or the credential kind (``CorrectionActor.kind``).
    actor_kind: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(Text)
    corrected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    old_summary: Mapped[str | None] = mapped_column(Text)
    new_summary: Mapped[str | None] = mapped_column(Text)
    #: The event's kind-specific facts (old/new status, conflict id).
    detail: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'preview'")
    )
    #: Readers past :data:`MAX_RECIPIENTS`, not listed.
    truncated: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Who sent it: ``user:<id>``, ``agent:<id>`` or the credential kind.
    sent_by: Mapped[str | None] = mapped_column(Text)
    #: When the bus delivery states were last read (a status read refreshes them
    #: at most every 30 s).
    bus_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            _in("kind", CORRECTION_KINDS), name="ck_memory_correction_kind"
        ),
        CheckConstraint(
            _in("status", CORRECTION_STATUSES), name="ck_memory_correction_status"
        ),
        CheckConstraint("truncated >= 0", name="ck_memory_correction_truncated"),
        # "Corrections of memory X", newest first; also backs the cascade.
        Index("ix_memory_correction_memory", "tenant_id", "memory_id", "corrected_at"),
        # The cascade from ``memories`` deletes by ``memory_id`` alone.
        Index("ix_memory_correction_memory_id", "memory_id"),
    )


class MemoryCorrectionRecipient(Base):
    __tablename__ = "memory_correction_recipient"

    correction_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("memory_correction.id", ondelete="CASCADE"),
        primary_key=True,
    )
    agent_id: Mapped[str] = mapped_column(Text, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``memory_exposure`` as it stood when the correction was recorded.
    reads: Mapped[int] = mapped_column(BigInteger, nullable=False)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_trace_id: Mapped[str | None] = mapped_column(Text)
    #: :data:`NOTICE_STATES`.
    notice_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    #: :data:`NOTICE_CHANNELS`; null until sent.
    channel: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: When the bus accepted the notice.
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: A throttled send waits until then.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    bus_message_id: Mapped[str | None] = mapped_column(Text)
    bus_delivery_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            _in("notice_state", NOTICE_STATES),
            name="ck_memory_correction_recipient_notice_state",
        ),
        CheckConstraint(
            _in("channel", NOTICE_CHANNELS),
            name="ck_memory_correction_recipient_channel",
        ),
        # The tenant purge deletes by tenant.
        Index("ix_memory_correction_recipient_tenant", "tenant_id"),
        # "Notices for agent X": the recall fallback (CP-13).
        Index("ix_memory_correction_recipient_agent", "tenant_id", "agent_id"),
    )
