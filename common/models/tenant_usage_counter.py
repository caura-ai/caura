"""Durable per-tenant, per-period usage counters (billing-grade).

The exact counterpart to :mod:`common.models.capability_usage`, and separate
from it on purpose. That table is adoption analytics: core-api buffers counts
in memory and flushes on a short interval, so anything since the last flush is
lost if the process dies, and rows are appended to be SUMmed with no unique
constraint. Both are right for "which capabilities get used" and wrong for
anything a plan cap is computed from — a lost flush is a customer under-billed
or over-served, and an unbounded append makes every cap check a growing SUM.

This one upserts: one row per ``(tenant_id, operation, period_start)``,
incremented atomically with ``ON CONFLICT DO UPDATE SET count = count +
excluded.count``. No buffering, no loss window, bounded rows, and a period
total that is a single-row read.

ROW PER OPERATION rather than a column per operation. The platform's
``enterprise.usage_counters`` has ``writes``/``searches``/``recalls`` columns,
which is why ``insights`` and ``evolve`` — both metered in core-api — have
nowhere to go there. Keying the operation as data takes a new one without a
migration.

Cross-tenant like ``capability_usage``, and for the same reason: it holds
counts and a ``tenant_id`` grouping dimension, never memory content. RLS is not
enabled on it. The platform reads it through core-storage-api's
``POST /tenant-usage/query`` — NOT by joining across schemas, even though both
live in one database; see that router's docstring for why.

Written via ``ServiceHooks.usage_meter``, so an OSS standalone deployment with
no meter wired records nothing and enforces nothing. See
``core_api.services.usage_service``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class TenantUsageCounter(Base):
    __tablename__ = "tenant_usage_counters"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Tenant the usage is attributed to. Grouping dimension, not an RLS key.
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    # One of core-api's metered operations: write | search | recall | insights
    # | evolve. Stored as data so a sixth needs no schema change.
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    # Start of the billing period, UTC. Supplied by the caller (month-truncated)
    # rather than derived here, so a row cannot land in a period other than the
    # one the caller believes it is counting, and a backfill can target a closed
    # period explicitly.
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        # Names and columns must match migration 039 exactly, so a
        # model-created schema (create_all / autogenerate) agrees with the
        # migration-created one.
        #
        # This unique index is the ``ON CONFLICT`` target. Without it the
        # upsert has nothing to bind to and concurrent writers quietly create
        # duplicate rows for one period — the append-and-SUM shape this table
        # exists to avoid.
        Index(
            "uq_tenant_usage_counters_key",
            "tenant_id",
            "operation",
            "period_start",
            unique=True,
        ),
        # The platform sweeps a whole period across tenants; the unique index
        # above already covers the tenant-scoped read.
        Index("ix_tenant_usage_counters_period", "period_start"),
    )
