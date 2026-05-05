"""Per-organization settings storage: live overrides + append-only audit trail.

Renamed from ``tenant_settings`` in CAURA-654. The keying column is
``org_id`` (text); semantically the value is the organization
identifier in enterprise deployments and the tenant identifier
acting as an implicit single-org key in OSS-standalone deployments
(no ``organizations`` table in pure OSS).
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class OrganizationSettings(Base):
    """Live organization overrides — one row per org, JSONB holds only overrides."""

    __tablename__ = "organization_settings"

    org_id: Mapped[str] = mapped_column(Text, primary_key=True)
    settings: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class OrganizationSettingsAudit(Base):
    """Append-only per-change audit trail. One row per PUT /settings that changes a value."""

    __tablename__ = "organization_settings_audit"
    __table_args__ = (
        Index(
            "idx_organization_settings_audit_org_created",
            "org_id",
            text("created_at DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(Text, nullable=False)
    changed_by: Mapped[str | None] = mapped_column(Text)
    diff: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
