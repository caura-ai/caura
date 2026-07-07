import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class AgentActivityDigest(Base):
    """One LLM-generated "what did this agent do" summary, per agent per window.

    Rows are grouped by ``run_id`` — a single scheduled pass over a tenant/fleet
    produces one row per agent. Unlike the live report (GET /api/v1/reports),
    these are PRECOMPUTED on a schedule (default nightly, see core-operations)
    and served read-only, because an LLM pass per agent is too slow/costly to
    run on every page view.

    ``status`` values:
      ok        — narrative generated from the agent's window memories
      quiet     — below the activity threshold; no LLM call made
      truncated — window exceeded the per-agent cap; summary is partial
      fallback  — LLM unavailable; narrative is a template placeholder
      skipped   — run-level cost/token cap hit before this agent
      error     — the LLM call (or fetch) failed; ``error_detail`` explains
    """

    __tablename__ = "agent_activity_digests"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=text("gen_random_uuid()"))
    # Groups every agent's row from one scheduled pass.
    run_id: Mapped[uuid.UUID] = mapped_column(nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    fleet_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    period: Mapped[str] = mapped_column(Text, nullable=False)  # 'day' | 'week'
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The prose summary + its structured breakdown
    # ({decisions[], shipped[], learned[], open_threads[]}).
    narrative: Mapped[str | None] = mapped_column(Text, nullable=True)
    sections: Mapped[dict] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # Provenance / quality signals surfaced in the UI.
    source_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )  # durable memories fed to the model
    recall_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)  # LLM registry id
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

    __table_args__ = (
        # Idempotent re-runs: one row per (tenant, fleet, agent, period, window).
        # TWO partial unique indexes rather than a single UniqueConstraint because
        # Postgres treats NULLs as distinct — a plain UNIQUE(... fleet_id ...) would
        # let duplicate no-fleet digests through whenever fleet_id IS NULL. Split on
        # the NULL-ness of fleet_id so uniqueness holds in both cases.
        Index(
            "uq_agent_digest_window_fleet",
            "tenant_id",
            "fleet_id",
            "agent_id",
            "period",
            "window_start",
            unique=True,
            postgresql_where=text("fleet_id IS NOT NULL"),
        ),
        Index(
            "uq_agent_digest_window_no_fleet",
            "tenant_id",
            "agent_id",
            "period",
            "window_start",
            unique=True,
            postgresql_where=text("fleet_id IS NULL"),
        ),
        # "latest run for this tenant/period" — the hot read path.
        Index(
            "ix_agent_digest_latest",
            "tenant_id",
            "period",
            text("window_start DESC"),
        ),
    )
