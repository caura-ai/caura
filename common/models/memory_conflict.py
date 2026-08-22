"""MemoryConflict — the unified contradiction-model conflict record (A55).

One row per detected candidate conflict between two memories. It captures the
model's three layers plus its evidence dimensions: the semantic *relationship*,
the diagnosed *cause*, the *evidence strength*, the chosen resolution *action*,
and a human-readable *audit_reason*. Classification is kept SEPARATE from the
applied state — the effect of the action still lives on the memory row
(``status`` / ``supersedes_id`` / ``weight``). See benchmark/A55-schema-design.md.

Enum-like columns are ``Text`` + ``CHECK`` (not native PG enums) so the vocabulary
can grow with a one-line constraint change during iteration.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base

RELATIONSHIPS = (
    "exact_value",
    "negation",
    "entailed",
    "constraint",
    "probabilistic",
    "scope_apparent",
    "refinement",
)
DIAGNOSES = (
    "correction",
    "temporal_change",
    "scope_difference",
    "entity_mismatch",
    "write_error",
    "unresolved",
)
EVIDENCE_STRENGTHS = ("explicit", "entailed", "probabilistic")
ACTIONS = (
    "replace",
    "supersede",
    "scope",
    "merge",
    "split_entity",
    "downweight",
    "mark_disputed",
    "ask",
    "no_op",
)


def _one_of(col: str, values: tuple[str, ...], nullable: bool = False) -> str:
    allowed = ", ".join(f"'{v}'" for v in values)
    clause = f"{col} IN ({allowed})"
    return f"{col} IS NULL OR {clause}" if nullable else clause


class MemoryConflict(Base):
    __tablename__ = "memory_conflicts"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    fleet_id: Mapped[str | None] = mapped_column(Text)

    # The two competing propositions. new = the later / triggering memory.
    new_memory_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
    )
    old_memory_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Layer 1 — semantic relationship (required).
    relationship: Mapped[str] = mapped_column(Text, nullable=False)
    relationship_confidence: Mapped[float | None] = mapped_column(Float)
    # Layer 2 — diagnosed cause (nullable = not yet diagnosed / unresolved).
    diagnosis: Mapped[str | None] = mapped_column(Text)
    diagnosis_confidence: Mapped[float | None] = mapped_column(Float)
    # Evidence basis of the conflict.
    evidence_strength: Mapped[str | None] = mapped_column(Text)
    # Layer 3 — chosen resolution action + why.
    action: Mapped[str | None] = mapped_column(Text)
    audit_reason: Mapped[str | None] = mapped_column(Text)

    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    # Provenance snapshot / scope overlap / temporal ordering / extra signals.
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)

    __table_args__ = (
        CheckConstraint(
            _one_of("relationship", RELATIONSHIPS),
            name="ck_memory_conflicts_relationship",
        ),
        CheckConstraint(
            _one_of("diagnosis", DIAGNOSES, nullable=True),
            name="ck_memory_conflicts_diagnosis",
        ),
        CheckConstraint(
            _one_of("evidence_strength", EVIDENCE_STRENGTHS, nullable=True),
            name="ck_memory_conflicts_evidence",
        ),
        CheckConstraint(
            _one_of("action", ACTIONS, nullable=True), name="ck_memory_conflicts_action"
        ),
        Index("ix_memory_conflicts_tenant", "tenant_id"),
        Index("ix_memory_conflicts_new", "new_memory_id"),
        Index("ix_memory_conflicts_old", "old_memory_id"),
    )
