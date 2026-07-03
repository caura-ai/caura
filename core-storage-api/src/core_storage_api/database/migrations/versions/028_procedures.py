"""Create ``procedures`` + ``procedure_stats`` tables (Procedural Memory PM-01).

The procedural-memory domain ported from Brain (procedural_memory_mcp).
A procedure is an executable, context-matched, reliability-ranked
tool-call sequence — distinct from a ``skills`` document (prose markdown
installed into a harness). Agents call ``memclaw_procedure_suggest`` with
their current ``context_features``, follow ``tools_sequence``, then close
the loop with ``memclaw_procedure_record``.

Two tables, 1:1:

  - ``procedures`` — the unit. Tenancy mirrors ``memories``
    (tenant_id / fleet_id / agent_id TEXT). ``embedding`` is the same
    ``Vector(VECTOR_DIM)`` (1024, bge-m3) the rest of the schema uses, so
    semantic ranking reuses MemClaw's embedder rather than Brain's 768-dim
    nomic vectors. ``skill_doc_id`` back-links to the ``documents`` row
    (collection='skills') a Forge bridge minted it from (PM-04); NULL for
    explicitly-captured procedures.
  - ``procedure_stats`` — reliability telemetry. The concrete
    implementation of the Skill Factory's deferred "Phase-4 outcome loop"
    (skill-factory-implementation-plan.md §15). ``record`` moves the
    counters, recomputes ``reliability_score``, and flips
    ``is_quarantined`` when a procedure proves unreliable.

``risk_level`` / ``status`` / ``visibility`` carry CHECK constraints as
defence-in-depth, mirroring ``021_session_traces`` (outcome_label CHECK)
and ``013_memory_type_check_constraint``.

Revision ID: 028
Revises: 027
Create Date: 2026-06-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

from common.constants import VECTOR_DIM

revision: str = "028"
down_revision: str | None = "027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "procedures",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("fleet_id", sa.Text(), nullable=True),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("pattern_signature", sa.Text(), nullable=False),
        sa.Column("tools_sequence", JSONB(), nullable=True),
        sa.Column(
            "context_features",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("embedding", Vector(VECTOR_DIM), nullable=True),
        sa.Column("reasoning_guide", sa.Text(), nullable=True),
        sa.Column(
            "risk_level",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'low'"),
        ),
        sa.Column(
            "is_canonical",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "precedence",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("skill_doc_id", sa.Text(), nullable=True),
        sa.Column(
            "visibility",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'scope_team'"),
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "risk_level IN ('low', 'medium', 'high')",
            name="ck_procedures_risk_level",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'candidate', 'staged', 'quarantined', 'stale', 'deprecated')",
            name="ck_procedures_status",
        ),
    )
    op.execute("CREATE INDEX ix_procedures_tenant_agent ON procedures (tenant_id, agent_id)")
    op.execute("CREATE INDEX ix_procedures_tenant_fleet ON procedures (tenant_id, fleet_id)")
    op.execute("CREATE INDEX ix_procedures_tenant_status ON procedures (tenant_id, status)")
    op.execute("CREATE INDEX ix_procedures_skill_doc ON procedures (skill_doc_id)")

    op.create_table(
        "procedure_stats",
        sa.Column(
            "procedure_id",
            PG_UUID(as_uuid=True),
            sa.ForeignKey("procedures.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "success_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "failure_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "reliability_score",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.5"),
        ),
        sa.Column(
            "is_quarantined",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("procedure_stats")
    op.drop_table("procedures")
