"""Create ``forge_rejected_fingerprints`` table (Skill Factory SF-003).

Anti-poison memory for the Forge resident. When an operator rejects a
staged skill candidate in the Skills Inbox, its
``cluster_fingerprint`` (``fp:v1:<sha256>``) lands here so Forge will
not re-propose the same cluster for ``cooloff_days`` (default 30).

Shape:

  - ``id``                UUID PRIMARY KEY — surrogate key; the same
                          fingerprint can be rejected multiple times
                          over its lifetime (e.g. re-rejected after a
                          cooloff expired and Forge re-proposed). One
                          row per rejection event preserves the audit
                          trail.
  - ``tenant_id``         TEXT NOT NULL — scoping key. Matches the
                          rest of the OSS schema (plain text, no FK).
  - ``fleet_id``          TEXT NULL — fingerprints are typically
                          fleet-scoped (one fleet's cluster is not the
                          same as another's even if the surface text
                          looks similar) but can be tenant-wide.
  - ``cluster_fingerprint`` TEXT NOT NULL — the rejected fingerprint,
                          format ``fp:v1:<sha256>``. NOT unique per
                          (tenant, fleet) — see ``id`` rationale above.
  - ``rejected_by_agent`` TEXT NOT NULL — operator identity from the
                          Inbox action. ``human:<userid>`` for UI
                          rejections, ``agent:<slug>`` if an automated
                          policy ever rejects (not in MVP).
  - ``rejected_at``       TIMESTAMPTZ NOT NULL DEFAULT now() — when
                          this rejection landed. Cooloff is computed
                          as ``rejected_at + cooloff_days``.
  - ``cooloff_days``      INTEGER NOT NULL DEFAULT 30 — how long Forge
                          must wait before re-proposing the same
                          fingerprint. Configurable per tenant via
                          ``org_settings.skills_factory.rejection_cooloff_days``
                          (snapshotted on insert; subsequent setting
                          changes do NOT retroactively affect existing
                          rows).
  - ``reason``            TEXT NULL — free-text operator reason from
                          the Reject action (optional). Surfaces in
                          audit views and inbox tooltips.

The hot-path query is "is this fingerprint poisoned RIGHT NOW for this
fleet?":

  SELECT EXISTS (
    SELECT 1 FROM forge_rejected_fingerprints
    WHERE tenant_id = :t
      AND (fleet_id = :f OR fleet_id IS NULL)
      AND cluster_fingerprint = :fp
      AND rejected_at + (interval '1 day' * cooloff_days) > now()
  )

The ``idx_forge_rejected_fp_lookup`` index covers it directly. The
``rejected_at DESC`` ordering lets the planner short-circuit on the
most recent rejection when scanning multiple historical rejections of
the same fingerprint.

Revision ID: 020
Revises: 019
Create Date: 2026-05-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID as PG_UUID

revision: str = "020"
down_revision: str | None = "019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "forge_rejected_fingerprints",
        sa.Column(
            "id",
            PG_UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("fleet_id", sa.Text(), nullable=True),
        sa.Column("cluster_fingerprint", sa.Text(), nullable=False),
        sa.Column("rejected_by_agent", sa.Text(), nullable=False),
        sa.Column(
            "rejected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "cooloff_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("30"),
        ),
        sa.Column("reason", sa.Text(), nullable=True),
    )
    # Hot-path: "is this fingerprint poisoned right now in this fleet?"
    #
    # The lookup query (Forge's CLI status-checker + Phase-2 candidate
    # gate) is:
    #
    #   WHERE tenant_id = :t
    #     AND cluster_fingerprint = :fp
    #     AND (fleet_id = :f OR fleet_id IS NULL)
    #     AND rejected_at + (interval '1 day' * cooloff_days) > now()
    #
    # ``fleet_id`` is part of the predicate, so it belongs in the
    # index — without it the planner has to do an extra filter step
    # over every (tenant, fp) match. ``NULLS FIRST`` clusters rows
    # with a NULL fleet_id at the start of each (tenant, fp) group,
    # which matches the ``fleet_id = :f OR fleet_id IS NULL`` shape
    # (PG can short-circuit the OR via the NULL bucket).
    #
    # Sorted DESC on rejected_at so the planner stops at the newest
    # hit per (tenant, fp, fleet) tuple — the cooloff-window predicate
    # only cares whether ANY rejection is still active.
    op.execute(
        "CREATE INDEX idx_forge_rejected_fp_lookup "
        "ON forge_rejected_fingerprints "
        "(tenant_id, cluster_fingerprint, fleet_id NULLS FIRST, rejected_at DESC)"
    )


def downgrade() -> None:
    op.drop_table("forge_rejected_fingerprints")
