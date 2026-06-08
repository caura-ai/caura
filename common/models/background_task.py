import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class BackgroundTaskLog(Base):
    __tablename__ = "background_task_log"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    task_name: Mapped[str] = mapped_column(Text, nullable=False)
    memory_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'failed'"),
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    error_traceback: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_bg_task_log_tenant_status", "tenant_id", "status"),
    )
