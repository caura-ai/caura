import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    fleet_id: Mapped[str | None] = mapped_column(Text)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    # ``display_name``: human-readable label shown in admin UIs / dashboards
    # / audit log columns. ``${hostname}-${baseName}`` from the plugin (e.g.
    # ``johnsmith-laptop-main``). Mutable — heartbeats refresh it so a
    # renamed machine propagates. Falls back to ``agent_id`` in the UI when
    # NULL (older plugin versions don't send the field).
    display_name: Mapped[str | None] = mapped_column(Text)
    # ``install_id``: per-plugin-install opaque suffix that disambiguates
    # the default ``"main"`` agent across multiple OpenClaw deployments
    # sharing the same tenant. The plugin generates this once at first
    # heartbeat and persists it to ``install.json`` in the plugin data dir;
    # never rotates. Indexed because the admin UI groups agents by install.
    install_id: Mapped[str | None] = mapped_column(String(32), index=True)
    trust_level: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    search_profile: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("uq_agents_tenant_agent", "tenant_id", "agent_id", unique=True),
    )
