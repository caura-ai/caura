"""Unified contradiction model (A55).

Adds the schema needed to represent the unified contradiction-handling model,
which today is flattened into ``memories.status`` + ``memories.supersedes_id``:

  * three columns on ``memories``:
      - ``confidence``   REAL, nullable — confidence in the memory's claim.
      - ``is_inferred``  BOOL, default false — system-materialised vs stated.
      - ``scope``        JSONB, default '{}' — validity qualifiers (role/task/location).
  * ``memory_conflicts`` — one conflict record per detected candidate conflict:
      relationship / diagnosis / evidence_strength / action + confidences + audit_reason.
      Enum-like columns are TEXT + CHECK so the vocabulary can grow cheaply.
  * ``memory_derivations`` — lineage: which upstream memory produced an inferred one.

No backfill: existing rows get ``confidence=NULL``, ``is_inferred=false``,
``scope='{}'``; no historical conflict records are created. See
benchmark/A55-schema-design.md.

Revision ID: 036
Revises: 035
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "036"
down_revision: str | None = "035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REL = "('exact_value','negation','entailed','constraint','probabilistic','scope_apparent','refinement')"
_DIAG = "('correction','temporal_change','scope_difference','entity_mismatch','write_error','unresolved')"
_EVID = "('explicit','entailed','probabilistic')"
_ACT = "('replace','supersede','scope','merge','split_entity','downweight','mark_disputed','ask','no_op')"


def upgrade() -> None:
    # ── memories: new columns (no backfill) ──
    op.add_column("memories", sa.Column("confidence", sa.Float(), nullable=True))
    op.add_column(
        "memories",
        sa.Column("is_inferred", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "memories",
        sa.Column("scope", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
    )

    # ── memory_conflicts ──
    op.create_table(
        "memory_conflicts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("fleet_id", sa.Text()),
        sa.Column(
            "new_memory_id", sa.Uuid(), sa.ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "old_memory_id", sa.Uuid(), sa.ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("relationship", sa.Text(), nullable=False),
        sa.Column("relationship_confidence", sa.Float()),
        sa.Column("diagnosis", sa.Text()),
        sa.Column("diagnosis_confidence", sa.Float()),
        sa.Column("evidence_strength", sa.Text()),
        sa.Column("action", sa.Text()),
        sa.Column("audit_reason", sa.Text()),
        sa.Column("created_by", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("metadata", postgresql.JSONB()),
        sa.CheckConstraint(f"relationship IN {_REL}", name="ck_memory_conflicts_relationship"),
        sa.CheckConstraint(
            f"diagnosis IS NULL OR diagnosis IN {_DIAG}", name="ck_memory_conflicts_diagnosis"
        ),
        sa.CheckConstraint(
            f"evidence_strength IS NULL OR evidence_strength IN {_EVID}", name="ck_memory_conflicts_evidence"
        ),
        sa.CheckConstraint(f"action IS NULL OR action IN {_ACT}", name="ck_memory_conflicts_action"),
    )
    op.create_index("ix_memory_conflicts_tenant", "memory_conflicts", ["tenant_id"])
    op.create_index("ix_memory_conflicts_new", "memory_conflicts", ["new_memory_id"])
    op.create_index("ix_memory_conflicts_old", "memory_conflicts", ["old_memory_id"])

    # ── memory_derivations ──
    op.create_table(
        "memory_derivations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("memory_id", sa.Uuid(), sa.ForeignKey("memories.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "source_memory_id", sa.Uuid(), sa.ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("memory_id", "source_memory_id", name="uq_memory_derivations_edge"),
    )
    op.create_index("ix_memory_derivations_memory", "memory_derivations", ["memory_id"])
    op.create_index("ix_memory_derivations_source", "memory_derivations", ["source_memory_id"])


def downgrade() -> None:
    op.drop_table("memory_derivations")
    op.drop_table("memory_conflicts")
    op.drop_column("memories", "scope")
    op.drop_column("memories", "is_inferred")
    op.drop_column("memories", "confidence")
