"""Per-fanout audit row for OSS scheduled lifecycle operations (CAURA-655).

One row is pre-published in the core-api fanout endpoint with
``status='pending'`` before the per-org Pub/Sub message goes out. The
core-worker consumer flips it to ``in_progress`` on receipt and to
``success`` / ``failure`` on completion. DLQ'd or never-consumed
messages remain observable as ``pending`` rows past their expected
finish time, and the reconcile sweep republishes those rows' messages
so the work completes rather than sitting observable-but-ignored.

``org_id`` is ``text`` and unconstrained — pure-OSS deployments key by
the standalone tenant id, enterprise deployments by the real org id.
Same shape as ``organization_settings.org_id`` (CAURA-654).
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base


class LifecycleAudit(Base):
    __tablename__ = "lifecycle_audit"
    __table_args__ = (
        # Supports cross-org recent-window health summaries. The older
        # org/action-leading index cannot serve a predicate on started_at
        # alone, so the smoke endpoint would otherwise scan the full audit
        # history as this append-only table grows.
        Index(
            "idx_lifecycle_audit_started_at",
            text("started_at DESC"),
        ),
        Index(
            "idx_lifecycle_audit_org_action_started",
            "org_id",
            "action",
            text("started_at DESC"),
        ),
        # The reconcile sweep looks for rows still at ``pending`` past
        # their expected finish. ``status='pending'`` is the selective
        # half of that predicate: ``started_at < now() - interval``
        # matches nearly the entire append-only history, so the recency
        # index above scans almost all of it to find the handful of rows
        # that never advanced. Partial on the status keeps this index
        # proportional to the stranded backlog -- normally zero rows --
        # rather than to the table.
        Index(
            "idx_lifecycle_audit_stranded",
            text("started_at"),
            postgresql_where=text("status = 'pending'"),
        ),
        # Partial index for the CAURA-657 dedup-gate query. Only
        # successful rows are indexed (status='success' partial),
        # keeping the index small while matching the dedup query
        # ordering exactly.
        Index(
            "idx_lifecycle_audit_dedup_gate",
            "org_id",
            "action",
            text("finished_at DESC"),
            postgresql_where=text("status = 'success'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    triggered_by: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    # When a consumer last claimed this row by moving it to ``in_progress``.
    # NULL means never claimed. The claim is what makes the transition
    # single-winner: two deliveries of the same ``audit_id`` -- an original
    # that was merely slow and the reconcile sweep's republish of it -- would
    # otherwise both flip ``pending`` to ``in_progress`` and run the primitive
    # at once. A stale value is re-claimable so a consumer that died mid-run
    # does not park the row forever.
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Who holds the claim above. One value per consumer INVOCATION, not per
    # HTTP request: the storage client retries a PATCH on ReadTimeout and 5xx,
    # so a claim that succeeded server-side but whose response was lost is
    # re-sent verbatim. Without an identity the compare-and-swap would read
    # that retry as a competing consumer and make the handler nack a delivery
    # it had in fact won. A genuine second delivery is a new invocation and
    # carries a different token, so it still loses.
    claim_token: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
