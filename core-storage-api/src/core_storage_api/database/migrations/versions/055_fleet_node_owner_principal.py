"""Bind each fleet node to the credential that may act as it.

M-85. A node was identified by its caller-supplied ``node_name`` alone, so any
write credential in the tenant could heartbeat as another node, take its queued
commands and report their results. ``owner_principal`` records which credential
the node answers to: ``tenant`` for a tenant-wide one, ``agent:<id>`` or
``install:<uuid>`` for a gateway-verified narrow one.

Nullable, and no backfill. Nothing recorded which credential an existing node
heartbeats with, and guessing would bind nodes to the wrong one. NULL means
"not bound yet": the node binds on its next heartbeat. Until then the first
credential in the tenant to heartbeat it claims it, pending commands included.
A tenant credential's next heartbeat takes it back, and the release endpoint
can bind it straight to a named credential. No index either: the
column is read on a row already found by its ``(tenant_id, node_name)``
constraint, or filtered within one tenant's nodes, which that constraint's index
already narrows to. ADD COLUMN of a nullable column with no default is
metadata-only in PostgreSQL 11+.

Revision ID: 055
Revises: 054
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "055"
down_revision: str | None = "054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("fleet_nodes", sa.Column("owner_principal", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("fleet_nodes", "owner_principal")
