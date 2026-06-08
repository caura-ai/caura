"""Add ``display_name`` and ``install_id`` columns to ``agents``.

Backs Task 6 of the API surface cleanup (CAURA-000-agent-display-name):
splits agent identity into a stable opaque ``agent_id`` (e.g.
``main-${installId}``) and a mutable human-readable ``display_name``
(e.g. ``johnsmith-laptop-main``) so multiple OpenClaw installs stop
colliding on the default ``"main"`` while UIs still show recognizable
hostnames.

Both columns are nullable with no default — older plugin versions
that don't send the new fields are unaffected; the admin UI falls
back to ``agent_id`` when ``display_name`` is NULL. The
``install_id`` column is indexed because the admin UI groups agents
by install.

Legacy ``agent_id="main"`` rows
-------------------------------
Pre-Task6 plugins all defaulted to ``agent_id="main"``. When an
upgraded plugin first heartbeats, it sends a fresh
``main-{install_id}`` and the server creates a new row — the legacy
``"main"`` row is intentionally NOT touched by this DDL (it's a data
concern, not a schema concern). Trust state carryover is handled in
``core_api.services.agent_service.get_or_create_agent``: when a fresh
``main-{install_id}`` row is created and a legacy ``"main"`` row
exists for the same tenant/fleet, ``trust_level`` and
``search_profile`` are copied forward into the new row.

Operators who want to consolidate or delete the legacy ``"main"``
rows after rollout can do so manually — the legacy rows stay
queryable so any memories still tagged with ``agent_id="main"``
remain accessible. Recovery snippets:

    -- Inspect what's left:
    SELECT tenant_id, fleet_id, count(*)
    FROM agents
    WHERE agent_id = 'main'
    GROUP BY 1, 2;

    -- Per-tenant rename in place (only if no upgraded plugin has
    -- already created a main-{install_id} row for the same fleet):
    UPDATE agents
       SET agent_id = 'main-' || install_id
     WHERE agent_id = 'main' AND install_id IS NOT NULL;

    -- Or archive: zero trust + mark display_name so it stops
    -- ranking in lists.
    UPDATE agents
       SET trust_level = 0,
           display_name = '[archived legacy main]'
     WHERE agent_id = 'main';

Revision ID: 010
Revises: 009
Create Date: 2026-04-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "010"
down_revision: str | None = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("display_name", sa.Text(), nullable=True))
    op.add_column("agents", sa.Column("install_id", sa.String(length=32), nullable=True))
    op.create_index(
        "ix_agents_install_id",
        "agents",
        ["install_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_agents_install_id", table_name="agents")
    op.drop_column("agents", "install_id")
    op.drop_column("agents", "display_name")
