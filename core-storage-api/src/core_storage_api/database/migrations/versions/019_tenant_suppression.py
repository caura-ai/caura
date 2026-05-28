"""Create ``tenant_suppression`` table (CAURA-694).

OSS-side mirror of the enterprise org-deletion lifecycle. The enterprise
``platform-admin-api`` publishes one
``memclaw.org.suppression-changed`` event per soft-delete + restore;
core-worker (or core-api in OSS standalone) subscribes and upserts one
row per affected tenant_id here. The core-api boundary guard reads this
table to reject reads/writes for suppressed tenants synchronously.

Shape:

  - ``tenant_id``    TEXT PRIMARY KEY — same shape as the rest of the
                     tenant-keyed OSS schema (no FK; standalone has no
                     tenants table). One row per tenant the lifecycle
                     has ever touched; restore CLEARS ``suppressed_at``
                     rather than deleting the row so the audit trail
                     (updated_at, updated_by) survives the round-trip.
  - ``suppressed_at`` TIMESTAMPTZ NULL — set on ``suppress``, cleared
                     on ``restore``. NULL ⇒ tenant is currently live.
                     A boundary check is exactly ``suppressed_at IS
                     NOT NULL`` — no enum, no second column to keep
                     in sync.
  - ``updated_at``    TIMESTAMPTZ NOT NULL DEFAULT now() — bumped on
                     every upsert (handled in the service layer).
  - ``updated_by``    TEXT NULL — caller identity from the event
                     payload (best-effort; absent for older messages).

No index beyond the primary key: lookups are point-reads by
``tenant_id`` on every authenticated request and PG's PK index is
enough; suppression-list queries are not on a hot path.

Revision ID: 019
Revises: 018
Create Date: 2026-05-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "019"
down_revision: str | None = "018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant_suppression",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        # NULL = live, NOT NULL = suppressed. Single source of truth so
        # the boundary check is a literal ``suppressed_at IS NOT NULL``.
        sa.Column("suppressed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("updated_by", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("tenant_suppression")
