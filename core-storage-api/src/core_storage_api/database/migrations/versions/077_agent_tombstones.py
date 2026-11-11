"""``agent_tombstones``: agents deleted from their tenant.

``DELETE /agents/{agent_id}`` removed the agent's row and nothing else, so the
next heartbeat, write or read naming the id registered it again at the default
trust. The delete now leaves a row here, and storage creates a tombstoned id at
trust 0 until an admin approves it; ``common/models/agent.py`` has the
contract.

A new, empty table in one transaction. No foreign key: the agent's row is gone.

Revision ID: 077
Revises: 076
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "077"
down_revision: str | None = "076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_tombstones",
        sa.Column("tenant_id", sa.Text(), primary_key=True),
        sa.Column("agent_id", sa.Text(), primary_key=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("agent_tombstones")
