"""``baton_recipient`` — which agents a handoff baton is waiting on.

Handoff batons (``docs/plans/handoff-batons.md``), D4. A baton is a bus request
(enterprise ``platform-collaboration-api``); the bus stores the baton itself as
a ``batons`` document and writes one row here per receiver when it sends it.
These rows are the recall fallback: an agent that is not listening on the bus
hears about a baton on its next recall (``baton_notices``), whatever it
searches for, until it accepts it or the baton is 7 days old.

``state`` is ``delivered`` until the receiver takes the baton up: ``accepted``
(``POST /batons/{id}/accept``, or an ack on the bus) or ``replied`` (a bus
reply). A recall shows only ``delivered`` rows. The bus delivery state is the
source of truth for a receiver on the bus; core-api copies it here, at most
every 30 s per row (``bus_checked_at``), after a recall that showed the row.

Standalone OSS has no bus, so nothing writes these rows and a recall finds
none. Ids and the goal only: the baton's decisions, questions and links stay
in the bus's document, which only its sender and receivers read.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base

#: ``delivered`` waits on the receiver; the other two are taken up.
BATON_STATES = ("delivered", "accepted", "replied")
#: Who settled it: the recall fallback's accept route, or the bus.
BATON_ACCEPTED_VIA = ("recall", "bus")
#: The schema's goal limit (D2).
GOAL_MAX_CHARS = 2000


class BatonRecipient(Base):
    __tablename__ = "baton_recipient"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    baton_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[str] = mapped_column(Text, primary_key=True)
    #: The sending agent (the baton's ``from_agent``).
    from_agent: Mapped[str] = mapped_column(Text, nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    #: When the baton was sent; a recall stops showing it 7 days later.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    #: The bus message that carried this receiver's copy.
    message_id: Mapped[str | None] = mapped_column(Text)
    #: :data:`BATON_STATES`.
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'delivered'")
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: :data:`BATON_ACCEPTED_VIA`; null while ``delivered``.
    accepted_via: Mapped[str | None] = mapped_column(Text)
    #: When the bus delivery state was last read for this row.
    bus_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "state IN ('delivered', 'accepted', 'replied')",
            name="ck_baton_recipient_state",
        ),
        CheckConstraint(
            "accepted_via IN ('recall', 'bus')", name="ck_baton_recipient_accepted_via"
        ),
        # "Batons waiting on agent X", newest first: the recall fallback. Partial,
        # so a taken-up baton leaves the index.
        Index(
            "ix_baton_recipient_waiting",
            "tenant_id",
            "agent_id",
            "created_at",
            postgresql_where=text("state = 'delivered'"),
        ),
    )
