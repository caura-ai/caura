"""Skill Factory ORM models — ``forge_rejected_fingerprints`` (SF-003) +
``session_traces`` (SF-004).

These tables were introduced migration-first (``020_forge_rejected_fingerprints``
+ ``021_session_traces``) and accessed via raw ``text()`` SQL. Fix 2 Ph5a
moves that access into core-storage-api; the models here mirror the migration
DDL so the table definitions live in SQLAlchemy metadata too — which is what
``Base.metadata.create_all`` (the test-schema path) builds from. Production
schema is still owned by the alembic migrations; keep both in lockstep.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class ForgeRejectedFingerprint(Base):
    """Anti-poison memory for the Forge resident (migration 020).

    One row per rejection event (the same fingerprint can be rejected
    multiple times); the cooloff-window check is satisfied by any live row.
    """

    __tablename__ = "forge_rejected_fingerprints"
    __table_args__ = (
        Index(
            "idx_forge_rejected_fp_lookup",
            "tenant_id",
            "cluster_fingerprint",
            text("fleet_id NULLS FIRST"),
            text("rejected_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    fleet_id: Mapped[str | None] = mapped_column(Text)
    cluster_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    rejected_by_agent: Mapped[str] = mapped_column(Text, nullable=False)
    rejected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    cooloff_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("30"))
    reason: Mapped[str | None] = mapped_column(Text)


class SessionTrace(Base):
    """Outcome-labeled session trace (migration 021).

    One row per ``(tenant_id, run_id, agent_id)``; re-runs of the outcome
    extractor upsert via the unique constraint.
    """

    __tablename__ = "session_traces"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "agent_id",
            name="uq_session_traces_tenant_run_agent",
        ),
        CheckConstraint(
            "outcome_label IN ('success', 'failure', 'unknown')",
            name="ck_session_traces_outcome_label",
        ),
        Index(
            "idx_session_traces_recent",
            "tenant_id",
            "fleet_id",
            text("ended_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    fleet_id: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    outcome_label: Mapped[str] = mapped_column(Text, nullable=False)
    memory_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    entity_ids: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    signals_summary: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    goal_phrase: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
