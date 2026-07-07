import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector  # type: ignore[import-not-found]
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from common.constants import VECTOR_DIM
from common.models.base import Base


class Procedure(Base):
    """A reusable, context-matched tool-call sequence.

    The procedural-memory unit ported from Brain (procedural_memory_mcp).
    Unlike a ``skills`` document (prose markdown installed into a harness),
    a procedure is an *executable* ranked sequence: agents call
    ``memclaw_procedure_suggest`` with their current ``context_features``,
    follow the returned ``tools_sequence``, then close the loop with
    ``memclaw_procedure_record``. Reliability lives in the 1:1
    ``procedure_stats`` row.

    Tenancy mirrors ``common.models.memory.Memory`` (tenant_id / fleet_id /
    agent_id as TEXT). ``skill_doc_id`` is the nullable back-link to the
    ``documents`` row (collection='skills') that a Forge bridge minted this
    procedure from; NULL for explicitly-captured procedures.
    """

    __tablename__ = "procedures"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    fleet_id: Mapped[str | None] = mapped_column(Text)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)

    name: Mapped[str] = mapped_column(Text, nullable=False)
    pattern_signature: Mapped[str] = mapped_column(Text, nullable=False)
    tools_sequence: Mapped[list | None] = mapped_column(JSONB)
    context_features: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    embedding = mapped_column(Vector(VECTOR_DIM))

    reasoning_guide: Mapped[str | None] = mapped_column(Text)
    risk_level: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'low'")
    )
    is_canonical: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    precedence: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    # Back-link to the skills document this was minted from (Forge bridge).
    skill_doc_id: Mapped[str | None] = mapped_column(Text)

    visibility: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'scope_team'")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'active'")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    stats: Mapped["ProcedureStats"] = relationship(
        "ProcedureStats",
        back_populates="procedure",
        uselist=False,
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("ix_procedures_tenant_agent", "tenant_id", "agent_id"),
        Index("ix_procedures_tenant_fleet", "tenant_id", "fleet_id"),
        Index("ix_procedures_tenant_status", "tenant_id", "status"),
        Index("ix_procedures_skill_doc", "skill_doc_id"),
    )


class ProcedureStats(Base):
    """Reliability telemetry for a procedure (1:1 with ``procedures``).

    This is the concrete implementation of the Skill Factory's deferred
    "Phase-4 outcome loop": ``memclaw_procedure_record`` moves these
    counters, recomputes ``reliability_score``, and flips
    ``is_quarantined`` when a procedure proves unreliable. The ranker in
    core-api skips quarantined procedures entirely.
    """

    __tablename__ = "procedure_stats"

    procedure_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("procedures.id", ondelete="CASCADE"),
        primary_key=True,
    )
    success_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # Loop Engineering LE-01: outcomes that were INDEPENDENTLY verified
    # (record called with validation_passed=True, from the harness's
    # separate evaluator agent). A subset of success_count/failure_count —
    # they distinguish a verified-reliable procedure from a self-reported
    # one (the paper's "Nodding Loop" defence). Telemetry only; quarantine
    # still keys off the combined counts.
    verified_success_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    verified_failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reliability_score: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0.5")
    )
    is_quarantined: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    procedure: Mapped["Procedure"] = relationship(
        "Procedure", back_populates="stats"
    )
