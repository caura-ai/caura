import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class CrystallizationReport(Base):
    __tablename__ = "analysis_reports"  # keep existing table name

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    fleet_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)  # "scheduled" | "manual"
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="running"
    )  # running | completed | failed
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    hygiene: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    health: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    usage_data: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    issues: Mapped[list] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    crystallization: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))

    __table_args__ = (
        Index("ix_analysis_reports_tenant_started", "tenant_id", started_at.desc()),
        Index("ix_analysis_reports_status", "status"),
    )
