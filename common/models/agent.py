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
    # ``owner_install_uuid``: the credential-install (``x-install-uuid``, the
    # 36-char value) that FIRST wrote memories as this agent. First-touch,
    # never overwritten (mirrors ``install_id``'s stability). SEPARATE from
    # ``install_id`` — this is the write-credential's install, used only for
    # the broker lenient ownership gate; ``install_id`` stays the plugin's
    # admin-grouping suffix. No index: read per-agent, never a query predicate.
    owner_install_uuid: Mapped[str | None] = mapped_column(String(36))
    trust_level: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    search_profile: Mapped[dict | None] = mapped_column(JSONB)
    # ``belonging_type`` / ``owner_ref``: the typed agent→owner relationship the
    # report API (GET /api/v1/reports) uses to resolve where an agent's report
    # goes. Two kinds:
    #   ``personal`` — belongs to a human user (``owner_ref`` = that user id);
    #                  the agent reports 1:1 to its owner.
    #   ``service``  — belongs to a group/fleet (the group IS ``fleet_id``;
    #                  ``owner_ref`` is NULL); the agent reports into the group.
    # Validated at the app layer (no DB enum, mirroring ``visibility``/``trust``).
    # Existing rows backfill to ``service`` so today's fleet agents keep working;
    # ``personal`` agents are set explicitly (interim ``owner_ref`` default =
    # the agent credential's ``created_by``). Extensible to more types later.
    belonging_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'service'")
    )
    owner_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("uq_agents_tenant_agent", "tenant_id", "agent_id", unique=True),
    )
