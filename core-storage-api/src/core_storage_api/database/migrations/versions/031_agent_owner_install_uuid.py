"""Add ``owner_install_uuid`` column to ``agents``.

Hardening broker-write attribution: records the credential-install
(``auth.install_uuid`` — the 36-char ``x-install-uuid``) that FIRST wrote
memories as this agent. First-touch and never overwritten, mirroring
``install_id``'s stability guarantee — but a SEPARATE field. ``install_id``
stays the plugin's admin-grouping suffix (``String(32)``); this holds a full
credential-install UUID (``String(36)``).

The broker bulk-write route stamps it on first contact and uses it for a
lenient ownership check: a broker write naming an agent whose
``owner_install_uuid`` is non-NULL and belongs to a *different* install is
degraded to the ``broker:<install>`` identity, so one install can't write
under another install's agent id.

Nullable, no default — existing rows and non-broker callers are unaffected.
No index: read per-agent via the ``(tenant_id, agent_id)`` lookup, never a
query predicate.

Revision ID: 031
Revises: 030
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "031"
down_revision: str | None = "030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("owner_install_uuid", sa.String(length=36), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "owner_install_uuid")
