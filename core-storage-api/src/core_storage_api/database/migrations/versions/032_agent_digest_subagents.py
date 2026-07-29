"""Add ``subagents`` column to ``agent_activity_digests``.

Subagent rollup (CAURA-222): a digest row now represents an agent *family* — the
parent plus any subagents whose work rolled up into it. ``subagents`` holds the
contributing children as ``[{agent_id, fleet_id, source_count}]`` so the report
can render them collapsed under the parent (and deep-link each into Prism).

NOT NULL default ``'[]'`` — existing rows and standalone agents carry an empty
list.

Revision ID: 032
Revises: 031
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "032"
down_revision: str | None = "031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_activity_digests",
        sa.Column("subagents", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("agent_activity_digests", "subagents")
