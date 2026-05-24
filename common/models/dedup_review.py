"""A1 #18 — dedup review queue rows.

One row per ambiguous dedup decision surfaced by
``CheckSemanticDuplicate``. See the migration docstring (018) for
the decision-band taxonomy.

``new_memory_id`` is nullable because rejected writes never persist
the row they were trying to create. The content snapshot
(``new_content``) is what the reviewer sees.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


# Status lifecycle:
#   pending → confirmed_duplicate (reviewer agreed; no-op as the
#             reject already happened OR the user accepts the
#             low-conf duplicate verdict)
#         → override_not_duplicate (reviewer disagrees with the
#             dedup decision; downstream tooling can use this to
#             re-submit or whitelist the pair)
#         → dismissed (reviewer doesn't want to see this one again
#             but doesn't take a position on the verdict)
DEDUP_REVIEW_STATUSES = (
    "pending",
    "confirmed_duplicate",
    "override_not_duplicate",
    "dismissed",
)

# Decision band — which CheckSemanticDuplicate tier produced this row.
DEDUP_REVIEW_BANDS = ("auto_reject", "judge_band_reject", "judge_low_conf_accept")


class DedupReview(Base):
    __tablename__ = "dedup_reviews"
    __table_args__ = (
        Index(
            "idx_dedup_reviews_tenant_status_created",
            "tenant_id",
            "status",
            text("created_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    fleet_id: Mapped[str | None] = mapped_column(Text)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    new_memory_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    candidate_memory_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    new_content: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_content: Mapped[str] = mapped_column(Text, nullable=False)
    similarity: Mapped[float] = mapped_column(Float, nullable=False)
    judge_verdict: Mapped[bool | None] = mapped_column(Boolean)
    judge_confidence: Mapped[float | None] = mapped_column(Float)
    decision_band: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
