"""Per-fanout audit row for OSS scheduled lifecycle operations (CAURA-655).

One row is pre-published in the core-api fanout endpoint with
``status='pending'`` before the per-org Pub/Sub message goes out. The
core-worker consumer flips it to ``in_progress`` on receipt and to
``success`` / ``failure`` on completion. DLQ'd or never-consumed
messages remain observable as ``pending`` rows past their expected
finish time.

``org_id`` is ``text`` and unconstrained — pure-OSS deployments key by
the standalone tenant id, enterprise deployments by the real org id.
Same shape as ``organization_settings.org_id`` (CAURA-654).
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class LifecycleAudit(Base):
    __tablename__ = "lifecycle_audit"
    __table_args__ = (
        Index(
            "idx_lifecycle_audit_org_action_started",
            "org_id",
            "action",
            text("started_at DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_by: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    stats: Mapped[dict | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
