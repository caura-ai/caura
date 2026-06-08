import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class FleetNode(Base):
    __tablename__ = "fleet_nodes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "node_name", name="uq_fleet_nodes_tenant_node"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    fleet_id: Mapped[str | None] = mapped_column(Text)
    node_name: Mapped[str] = mapped_column(Text, nullable=False)
    hostname: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(Text)
    openclaw_version: Mapped[str | None] = mapped_column(Text)
    plugin_version: Mapped[str | None] = mapped_column(Text)
    plugin_hash: Mapped[str | None] = mapped_column(Text)
    os_info: Mapped[str | None] = mapped_column(Text)
    agents_json: Mapped[dict | None] = mapped_column(JSONB)
    tools_json: Mapped[dict | None] = mapped_column(JSONB)
    channels_json: Mapped[dict | None] = mapped_column(JSONB)
    extra: Mapped[dict | None] = mapped_column("metadata", JSONB)
    last_heartbeat: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class FleetCommand(Base):
    __tablename__ = "fleet_commands"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fleet_nodes.id", ondelete="CASCADE"), nullable=False
    )
    command: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
