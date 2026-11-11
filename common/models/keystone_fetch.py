"""First rule fetch per install/principal/fleet/hash/minute; retained for 90 days."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class KeystoneFetch(Base):
    __tablename__ = "keystone_fetches"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    install_uuid: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    credential_id: Mapped[str] = mapped_column(Text, nullable=False)
    principal: Mapped[str] = mapped_column(Text, nullable=False)
    # The empty string represents tenant-wide scope, including in the unique key.
    fleet_id: Mapped[str] = mapped_column(Text, nullable=False)
    rule_set_hash: Mapped[str] = mapped_column(Text, nullable=False)
    bucket_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    served_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "install_uuid",
            "principal",
            "fleet_id",
            "rule_set_hash",
            "bucket_start",
            name="uq_keystone_fetch_bucket",
        ),
        CheckConstraint(
            "rule_set_hash ~ '^[0-9a-f]{64}$'", name="ck_keystone_fetch_hash"
        ),
        CheckConstraint(
            "served_at >= bucket_start AND served_at < bucket_start + interval '60 seconds'",
            name="ck_keystone_fetch_time",
        ),
        Index("ix_keystone_fetch_retention", "served_at"),
    )
