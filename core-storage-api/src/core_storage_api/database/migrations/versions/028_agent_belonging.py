"""Agent belonging model: ``belonging_type`` + ``owner_ref`` on ``agents``.

Adds the typed agent→owner relationship the report API (GET /api/v1/reports)
uses to decide where an agent's report is delivered:

  belonging_type='personal' → belongs to a human user (``owner_ref`` = user id);
                              the agent reports 1:1 to its owner.
  belonging_type='service'  → belongs to a group/fleet (the group IS ``fleet_id``;
                              ``owner_ref`` NULL); the agent reports into the group.

Existing rows backfill to ``service`` (server_default) with ``owner_ref`` NULL —
today's fleet agents keep working unchanged; ``personal`` agents are set
explicitly. No DB enum/CHECK: the value set is validated at the app layer,
mirroring ``memories.visibility`` and ``agents.trust_level``.

Revision ID: 028
Revises: 027
Create Date: 2026-06-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "028"
down_revision: str | None = "027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "belonging_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'service'"),
        ),
    )
    op.add_column("agents", sa.Column("owner_ref", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "owner_ref")
    op.drop_column("agents", "belonging_type")
