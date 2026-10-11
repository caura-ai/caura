"""Per-minute model-call and savings totals (usage metering).

``llm_usage_minutes``: one row per minute x tenant x agent x fleet x operation
x kind x provider x model, adding up calls, failed attempts and the
provider-reported tokens in, out and reasoning. Written by core-api and
core-worker from an in-memory buffer every ~15 s (``common.usage``), as an
additive upsert, so several instances metering the same minute all land.

``value_usage_minutes``: the savings measures Caura can stand behind
(``recall.returned_tokens``, ``recall.source_tokens``,
``recall.cross_agent_results``, ``hygiene.retired_tokens`` and the like), one
row per minute x tenant x agent x fleet x operation x metric.

``usage_flush_batches``: the idempotency key for a flush. A buffer that was
sent but whose reply was lost is re-sent with the same id and counted once,
which is what lets the writer retry instead of choosing between losing counts
and double-counting them.

Counts only, never content. Cross-tenant and RLS-free like
``tenant_usage_counters``: one flush carries many tenants and each row names
its own ``tenant_id``. An unknown agent or fleet is ``''``, not NULL, so it can
sit in the unique key.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class LlmUsageMinute(Base):
    __tablename__ = "llm_usage_minutes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    fleet_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    #: ``llm`` or ``embedding``.
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    #: Provider responses received (billed).
    calls: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    #: Attempts that raised, timeouts included; no tokens.
    failures: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    tokens_in: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    tokens_out: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    #: Reasoning/thinking tokens, billed as output but never in the visible text.
    tokens_reasoning: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint(
            "calls >= 0 AND failures >= 0 AND tokens_in >= 0 AND tokens_out >= 0 AND tokens_reasoning >= 0",
            name="ck_llm_usage_minutes_nonneg",
        ),
        # The ON CONFLICT target; names and columns must match migration 066.
        Index(
            "uq_llm_usage_minutes_key",
            "minute",
            "tenant_id",
            "agent_id",
            "fleet_id",
            "operation",
            "kind",
            "provider",
            "model",
            unique=True,
        ),
        Index("ix_llm_usage_minutes_tenant_minute", "tenant_id", "minute"),
    )


class ValueUsageMinute(Base):
    __tablename__ = "value_usage_minutes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    fleet_id: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    operation: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))

    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_value_usage_minutes_nonneg"),
        Index(
            "uq_value_usage_minutes_key",
            "minute",
            "tenant_id",
            "agent_id",
            "fleet_id",
            "operation",
            "metric",
            unique=True,
        ),
        Index("ix_value_usage_minutes_tenant_minute", "tenant_id", "minute"),
    )


class UsageFlushBatch(Base):
    __tablename__ = "usage_flush_batches"

    batch_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (Index("ix_usage_flush_batches_received_at", "received_at"),)
