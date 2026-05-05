"""Rename ``tenant_settings`` and ``tenant_settings_audit`` to
``organization_settings`` and ``organization_settings_audit``, swapping
the keying column from ``tenant_id`` to ``org_id``.

Why. All current settings (crystallizer, security_audit, lifecycle,
etc.) are conceptually per-organization. The legacy per-tenant table
holds zero rows in prod (verified empirically) and the admin UI is
already org-scoped. Moving storage to match the conceptual model now
avoids a fan-out / migration story later when settings become
populated.

OSS-standalone compatibility. Pure-OSS deployments don't have an
``organizations`` table — that's an enterprise-side concept. The
``org_id`` column stays ``text`` (matching today's ``tenant_id text``
shape) with NO foreign-key constraint, so the schema works in both:

* OSS standalone: callers pass ``tenant_id`` as the org-key (single
  implicit org per tenant — no real org concept exists).
* Enterprise: callers pass the real ``org_id`` (string-form UUID from
  the user's auth context); multiple member tenants under the same org
  share one settings row, which is the desired behaviour.

Migration shape: rename → create → copy → drop. Even though prod has
zero rows today, the pattern is data-preserving by construction so any
local/dev DB with overrides survives the upgrade. Each step:

  1. Rename ``tenant_settings`` → ``tenant_settings_old`` (and audit
     siblings). The rename is atomic and preserves data and indexes.
  2. Create the new ``organization_settings`` / ``..._audit`` tables.
  3. ``INSERT … SELECT`` from the renamed legacy tables — no-op when
     empty, copies rows verbatim when present. The audit table's
     ``id`` is intentionally not preserved so the new table starts
     its sequence at 1; audit ordering is via ``created_at`` and
     nothing references ``audit.id`` as a foreign key.
  4. Drop the renamed legacy tables. Whole migration is one
     transaction (alembic default), so a failure between any two
     steps rolls back cleanly.

``downgrade()`` is symmetric — rename forward → create legacy → copy
back → drop forward — so time-travel testing works either direction.

Revision ID: 014
Revises: 013
Create Date: 2026-05-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "014"
down_revision: str | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenant_settings RENAME TO tenant_settings_old")
    op.execute("ALTER TABLE tenant_settings_audit RENAME TO tenant_settings_audit_old")
    op.execute(
        "ALTER INDEX idx_tenant_settings_audit_tenant_created "
        "RENAME TO idx_tenant_settings_audit_old_tenant_created"
    )

    op.create_table(
        "organization_settings",
        sa.Column("org_id", sa.Text(), primary_key=True),
        sa.Column("settings", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "organization_settings_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("org_id", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.Text()),
        sa.Column("diff", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(
        "CREATE INDEX idx_organization_settings_audit_org_created "
        "ON organization_settings_audit (org_id, created_at DESC)"
    )

    op.execute(
        "INSERT INTO organization_settings (org_id, settings, updated_at) "
        "SELECT tenant_id, settings, updated_at FROM tenant_settings_old"
    )
    op.execute(
        "INSERT INTO organization_settings_audit (org_id, changed_by, diff, created_at) "
        "SELECT tenant_id, changed_by, diff, created_at FROM tenant_settings_audit_old"
    )

    op.drop_table("tenant_settings_audit_old")
    op.drop_table("tenant_settings_old")


def downgrade() -> None:
    op.execute("ALTER TABLE organization_settings RENAME TO organization_settings_old")
    op.execute("ALTER TABLE organization_settings_audit RENAME TO organization_settings_audit_old")
    op.execute(
        "ALTER INDEX idx_organization_settings_audit_org_created "
        "RENAME TO idx_organization_settings_audit_old_org_created"
    )

    op.create_table(
        "tenant_settings",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("settings", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_table(
        "tenant_settings_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("changed_by", sa.Text()),
        sa.Column("diff", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.execute(
        "CREATE INDEX idx_tenant_settings_audit_tenant_created "
        "ON tenant_settings_audit (tenant_id, created_at DESC)"
    )

    op.execute(
        "INSERT INTO tenant_settings (tenant_id, settings, updated_at) "
        "SELECT org_id, settings, updated_at FROM organization_settings_old"
    )
    op.execute(
        "INSERT INTO tenant_settings_audit (tenant_id, changed_by, diff, created_at) "
        "SELECT org_id, changed_by, diff, created_at FROM organization_settings_audit_old"
    )

    op.drop_table("organization_settings_audit_old")
    op.drop_table("organization_settings_old")
