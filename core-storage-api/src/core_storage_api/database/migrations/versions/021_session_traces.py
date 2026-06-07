"""Create ``session_traces`` table (Skill Factory SF-004).

Outcome-labeled session traces. Populated by the outcome inference
layer (Phase 1) from the 6 free signals — contradictions,
supersessions, repeat-recall, terminal memory, cross-agent reuse,
external hooks. Forge reads from this table to build skill candidate
clusters.

A session-trace is the unit Forge mines. It groups the memories an
agent wrote during one run/task into a single object tagged with an
outcome label inferred without any ``memclaw_evolve`` call. Multiple
traces sharing a goal + entity overlap become a skill cluster.

Shape:

  - ``id``                UUID PRIMARY KEY — surrogate key.
  - ``tenant_id``         TEXT NOT NULL — scoping key, plain text per
                          the rest of the OSS schema.
  - ``fleet_id``          TEXT NULL — most traces are fleet-scoped;
                          NULL allowed for tenant-wide standalone.
  - ``run_id``            TEXT NOT NULL — groups the memories that
                          belong to this trace. Already present on
                          memory writes (017_memories_run_id_index).
  - ``agent_id``          TEXT NOT NULL — author of the trace.
  - ``outcome_label``     TEXT NOT NULL — ``success`` | ``failure`` |
                          ``unknown``. Inferred passively; never set
                          by an agent directly.
  - ``memory_ids``        JSONB NOT NULL DEFAULT '[]'::jsonb — ordered
                          array of memory UUIDs that made up this
                          trace. Stored as a JSONB array (not a
                          relation) because traces are immutable
                          snapshots and per-trace queries always read
                          the whole list at once. Lets us survive
                          downstream memory deletes without dangling FKs
                          (deleted memory ids are tolerated and skipped
                          at read time).
  - ``entity_ids``        JSONB NOT NULL DEFAULT '[]'::jsonb — entity
                          UUIDs touched by the trace (from the existing
                          entity-resolution layer). Drives Forge's
                          cluster centroid via top-K by centrality.
  - ``signals_summary``   JSONB NOT NULL DEFAULT '{}'::jsonb — per-
                          signal evidence ({contradiction: [...],
                          supersession: [...], repeat_recall: 0|n,
                          terminal: "shipped"|"blocked"|null,
                          cross_agent_reuse: int, external: {git: …,
                          ci: …}}). Free-shape so individual signal
                          extractors can evolve without a migration.
  - ``goal_phrase``       TEXT NULL — LLM-extracted 6-word phrase
                          describing what the trace was about.
                          Populated lazily by the cluster fingerprint
                          step; NULL until then.
  - ``started_at``        TIMESTAMPTZ NOT NULL — earliest member
                          memory ``created_at``.
  - ``ended_at``          TIMESTAMPTZ NOT NULL — latest member memory
                          ``created_at`` (terminal memory or last
                          write in window, whichever is later).
  - ``created_at``        TIMESTAMPTZ NOT NULL DEFAULT now() — when
                          the outcome inference layer wrote this row.

Unique constraint on ``(tenant_id, run_id, agent_id)``: one trace per
run + agent. If the same run_id is shared by multiple agents (rare
but possible), each agent gets its own trace row. Re-runs of the
outcome extractor against the same run upsert via this constraint.

Two query patterns drive the indexes:

  1. Forge "recent traces in this fleet" — idx_session_traces_recent
     (tenant_id, fleet_id, ended_at DESC). Bounded by
     freshness_window_days (default 14d).
  2. Outcome extractor's "is this trace already labeled" lookup —
     covered by the unique constraint's implicit index.

Revision ID: 021
Revises: 020
Create Date: 2026-05-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "021"
down_revision: str | None = "020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_traces",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("fleet_id", sa.Text(), nullable=True),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=False),
        sa.Column("outcome_label", sa.Text(), nullable=False),
        sa.Column(
            "memory_ids",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "entity_ids",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "signals_summary",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("goal_phrase", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "agent_id",
            name="uq_session_traces_tenant_run_agent",
        ),
        # Defence-in-depth: outcome_label is application-managed but a
        # CHECK keeps it honest at the DB layer. Mirrors the
        # ``013_memory_type_check_constraint`` pattern.
        sa.CheckConstraint(
            "outcome_label IN ('success', 'failure', 'unknown')",
            name="ck_session_traces_outcome_label",
        ),
    )
    # Forge's primary read: "recent traces in this fleet within the
    # freshness window". DESC on ended_at because freshness is
    # decided from the trace end, not start.
    op.execute(
        "CREATE INDEX idx_session_traces_recent ON session_traces (tenant_id, fleet_id, ended_at DESC)"
    )


def downgrade() -> None:
    op.drop_table("session_traces")
