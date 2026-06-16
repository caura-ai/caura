import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, LargeBinary, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[uuid.UUID | None] = mapped_column()
    detail: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    # ── Tamper-evident hash chain (migration 025) ────────────────────
    # Per-tenant linked-hash chain. These are NULL on pre-migration rows
    # (legacy append-only): the chain genesis is the migration boundary,
    # so existing rows are intentionally left unchained and excluded from
    # verification (a partial unique index covers ``seq IS NOT NULL`` only).
    # Chained rows carry a monotonic per-tenant ``seq`` (starts at 1), the
    # prior event's ``event_hash`` as ``prev_hash`` (a genesis sentinel for
    # seq=1), and ``event_hash = SHA256(canonical_event || prev_hash)``.
    seq: Mapped[int | None] = mapped_column(BigInteger)
    prev_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    event_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    # ── Per-event idempotency key (migration 026) ────────────────────
    # A UUID minted per event by core-api's ``log_action``. Storage dedups
    # on it (under the per-tenant head lock, BEFORE seq assignment) so the
    # async audit bulk flush can safely retry a lost-ack POST without
    # double-appending to the chain. NULL on the legacy single-event path
    # and pre-026 rows (the partial unique index covers NON-NULL only).
    client_event_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        # Backs per-event audit-flush idempotency (migration 026). Created
        # ``CONCURRENTLY`` there with the same predicate; declared here so
        # SQLAlchemy reflection / Alembic autogen round-trips match the live
        # schema. NULL ``client_event_id`` rows are excluded so the legacy
        # single-event path and pre-026 rows are unaffected.
        Index(
            "ix_audit_log_client_event_id_unique",
            "tenant_id",
            "client_event_id",
            unique=True,
            postgresql_where=text("client_event_id IS NOT NULL"),
        ),
    )


class AuditChainHead(Base):
    """Per-tenant serialization point for the audit hash chain.

    One row per tenant — locked ``FOR UPDATE`` during a chained insert so
    concurrent same-tenant batches serialize against each other without
    taking a lock on the (large, append-only) ``audit_log`` table, and
    different tenants never block one another. ``last_hash`` is the
    ``event_hash`` of the most recent chained event (a genesis sentinel
    when ``last_seq=0``); the next insert chains onto it.
    """

    __tablename__ = "audit_chain_head"

    tenant_id: Mapped[str] = mapped_column(Text, primary_key=True)
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    last_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
