"""Counts and reference-only retries for tenant background model work."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class BackgroundModelBudget(Base):
    __tablename__ = "background_model_budgets"
    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    __table_args__ = (
        CheckConstraint("used >= 0", name="ck_background_model_budget_used"),
    )


class BackgroundModelReservation(Base):
    __tablename__ = "background_model_reservations"
    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    attempt_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    # Null only for a release tombstone that preceded admission.
    admitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    released: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


class BackgroundModelWork(Base):
    __tablename__ = "background_model_work"
    # Put the byte-order contract on the column so PostgreSQL reflection and
    # Alembic preserve it without an index-only COLLATE expression mismatch.
    tenant_id: Mapped[str] = mapped_column(Text(collation="C"), primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    memory_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    lease_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    terminal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (
        CheckConstraint(
            "terminal_reason IS NULL OR terminal_reason = 'retry_exhausted'",
            name="ck_background_model_work_terminal_reason",
        ),
        Index(
            "ix_background_model_work_due",
            "available_at",
            "lease_until",
            postgresql_where=text("terminal_reason IS NULL"),
        ),
        Index(
            "ix_background_model_work_tenant_due",
            "tenant_id",
            "available_at",
            "lease_until",
            postgresql_where=text("terminal_reason IS NULL"),
        ),
    )
