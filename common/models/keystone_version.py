from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class KeystoneVersion(Base):
    """One change to a tenant's keystones (migration 061).

    Versions count per tenant from 1, one for each keystone set or delete, in
    the write's own transaction. ``snapshot`` holds the tenant's whole keystone
    set after the change, so an agent's rule set at any version is that
    snapshot resolved as ``GET /keystones`` resolves the live set. Version 1 of
    a tenant that had keystones before versioning is a ``baseline`` of them.
    """

    __tablename__ = "keystone_versions"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    # ``set`` or ``delete``; ``baseline``, the set versioning started from; or
    # ``resync``, a change that reached the set without a version, recorded
    # at the tenant's next write.
    op: Mapped[str] = mapped_column(Text, nullable=False)
    # The rule that changed; NULL for a baseline or a resync.
    doc_id: Mapped[str | None] = mapped_column(Text)
    # One object per keystone: ``doc_id``, ``fleet_id``, ``data`` and
    # ``updated_at``, the fields the list resolves, orders and hashes by.
    # ``json``, as ``documents.data`` is: JSONB refuses a ``\u0000`` escape.
    snapshot: Mapped[list] = mapped_column(JSON, nullable=False)
    # Who made the change: the calling agent, and the person the gateway
    # vouched for. NULL when unknown, and for a baseline or a resync.
    actor_agent_id: Mapped[str | None] = mapped_column(Text)
    actor_user_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
