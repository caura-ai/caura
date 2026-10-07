"""``keystone_versions``: a tenant's keystone set after each change.

Keystones had no history: an upsert replaces its document in place and a delete
removes it, so nothing could say which rules an agent had last Tuesday, or since
when it has had today's. Each keystone set or delete now records a version in
its own transaction (``services/keystones.py``), numbered per tenant from 1.

ONE COUNTER PER TENANT, AND THE WHOLE SET. A version stores the tenant's every
keystone after the change, not just the changed one. An agent's rules at any
version are then that snapshot resolved for its fleet and itself, as
``GET /keystones`` resolves the live set, so one row answers for every agent and
a version number counts the tenant's changes.

THE SNAPSHOT keeps per keystone only what the list resolves, orders and hashes
by: ``doc_id``, ``fleet_id``, ``data`` and ``updated_at``, the last in UTC with
six fraction digits, as the rule-set hash writes it. It is ``json``, built as
text and never parsed, as ``documents.data`` is: JSONB refuses the escape for a
NUL character, which a keystone's text may hold, and one such keystone would
fail this migration.

BASELINE. Every tenant that already has keystones gets version 1, op
``baseline``: the set versioning starts from, with no changed rule and no actor.
``BASELINE_SQL`` skips a tenant that already has versions, so running it again
adds nothing. A keystone written by a storage instance still on the old code
while this migrates reaches the set without a version; the tenant's next write
records it first, as a version of its own with op ``resync``.

NO SECONDARY INDEX. Every read is one tenant's versions by number, which the
primary key serves.

Revision ID: 061
Revises: 060
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSON

revision: str = "061"
down_revision: str | None = "060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BASELINE_SQL = """
INSERT INTO keystone_versions (tenant_id, version, op, doc_id, snapshot)
SELECT d.tenant_id, 1, 'baseline', NULL,
       json_agg(
           json_build_object(
               'doc_id', d.doc_id,
               'fleet_id', d.fleet_id,
               'data', d.data,
               'updated_at',
               to_char(d.updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
           )
           ORDER BY d.doc_id COLLATE "C"
       )
FROM documents d
WHERE d.collection = '_keystones'
  AND NOT EXISTS (SELECT 1 FROM keystone_versions v WHERE v.tenant_id = d.tenant_id)
GROUP BY d.tenant_id
"""


def upgrade() -> None:
    op.create_table(
        "keystone_versions",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("op", sa.Text(), nullable=False),
        sa.Column("doc_id", sa.Text(), nullable=True),
        sa.Column("snapshot", JSON(), nullable=False),
        sa.Column("actor_agent_id", sa.Text(), nullable=True),
        sa.Column("actor_user_id", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("tenant_id", "version"),
    )
    op.execute(BASELINE_SQL)


def downgrade() -> None:
    op.drop_table("keystone_versions")
