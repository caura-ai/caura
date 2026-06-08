"""Idempotency inbox for Idempotency-Key replay (CAURA-601).

A row per (tenant_id, idempotency_key) stores the request body hash
and the response to replay. The hash guards against the same key
being reused with a different body (which the client treats as a
conflict) — that's the contract every idempotency-key standard
(IETF draft, Stripe) agrees on.

A short TTL keeps the table bounded; opportunistic cleanup or a
periodic job (tracked separately) prunes expired rows.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class IdempotencyResponse(Base):
    __tablename__ = "idempotency_responses"
    __table_args__ = (
        PrimaryKeyConstraint(
            "tenant_id", "idempotency_key", name="pk_idempotency_responses"
        ),
        # TTL cleanup job scans by expires_at.
        Index("ix_idempotency_responses_expires_at", "expires_at"),
    )

    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    response_body: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # ``true`` while the handler is still running, flipped to ``false``
    # by ``idempotency_record`` once the response body is durable.
    # Closes the race where two concurrent requests with the same key
    # both miss the cache and both execute the handler.
    is_pending: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
