"""Capability-usage analytics counters.

Append-only aggregate table answering "which product capabilities get
used, by access path (transport), and by org" — the data behind the
adoption report. Written by core-api's in-process aggregator
(``services/capability_usage.py``), which collapses many requests into
one row per ``(tenant_id, capability, op, transport, ts_bucket)`` and
flushes on a short interval. Multiple core-api instances each append
their own rows; consumers SUM at query time, so there is intentionally
NO unique constraint and NO upsert (append-only, contention-free).

This is an internal analytics table:
  * It is deliberately CROSS-TENANT — the whole point is to aggregate
    adoption across orgs — so RLS is NOT enabled on it (unlike the
    tenant-scoped data tables). ``tenant_id`` here is a grouping
    dimension, not an RLS key.
  * It holds only counts + latency sums, never memory content. The only
    identifier is ``tenant_id`` (org), which is why per-org breakdowns
    stay on the internal/ops side rather than in a customer surface.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class CapabilityUsage(Base):
    __tablename__ = "capability_usage"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Org the usage is attributed to. Grouping dimension, not an RLS key.
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Normalized capability name, shared across transports (e.g. "recall",
    # "write", "doc", "keystones") so MCP and REST roll up together.
    capability: Mapped[str] = mapped_column(Text, nullable=False)
    # Sub-operation for multiplexed capabilities (e.g. doc.search vs
    # doc.write, manage.read vs manage.delete). NULL when the capability
    # has no sub-ops.
    op: Mapped[str | None] = mapped_column(Text)
    # Access path: "mcp" | "rest".
    transport: Mapped[str] = mapped_column(Text, nullable=False)
    # Start of the time bucket this row aggregates (minute-truncated, UTC).
    # Rows sharing a bucket across flushes/instances SUM cleanly at query.
    ts_bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    error_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # Sum of observed durations in the bucket; mean = duration_ms_sum / count.
    # (Percentiles need a histogram — a deliberate follow-up, not v1.)
    duration_ms_sum: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    # Wall-clock flush time of the appending instance — debugging aid only.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        # Must match migration 023's constraint name + predicate exactly so
        # model-created tables (create_all / autogenerate) agree with the
        # migration-created schema.
        CheckConstraint(
            "transport IN ('mcp', 'rest')", name="ck_capability_usage_transport"
        ),
        # Time-range scans for the adoption report (group by capability/transport).
        Index("ix_capability_usage_bucket", "ts_bucket"),
        # Per-org drilldown.
        Index("ix_capability_usage_tenant_bucket", "tenant_id", "ts_bucket"),
    )
