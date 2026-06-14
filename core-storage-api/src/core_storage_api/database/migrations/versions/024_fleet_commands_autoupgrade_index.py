"""Add partial btree index on ``fleet_commands (node_id, created_at)``
WHERE ``command = 'deploy'`` to back the auto-upgrade gate queries.

CAURA-000: the heartbeat auto-upgrade gate (``routes/fleet.py``) runs two
per-node deploy lookups — ``has_recent_in_flight_deploy`` (pending/acked
in the last 10 min) and the new ``count_recent_deploys_for_target``
(attempt budget per target over 24 h). Both filter
``node_id = ? AND command = 'deploy' AND created_at >= ?``. The only
pre-existing index on the table is ``ix_fleet_commands_tenant_id``
(``tenant_id`` alone), so both queries sequential-scan all of a node's
historical commands. At today's volume (~1.4 k rows on the eToro prod
box) that's trivial, but ``fleet_commands`` grows with
fleet_size x command_rate x retention, so this lands the index before it
matters rather than after.

Partial on ``command = 'deploy'``: deploy is a small fraction of all
fleet commands (heartbeats also queue educate / ping / restart / skill
reconcile), so excluding non-deploy rows keeps the index compact. The
JSONB ``payload->>'target_version'`` predicate is intentionally NOT in
the index: the (node_id, created_at, partial) seek reduces the
candidate set to O(1)-O(10) rows, and the JSONB extract is then a cheap
filter on those.

CONCURRENTLY: plain ``CREATE INDEX`` takes an AccessExclusiveLock that
blocks all writes (including the heartbeat command-insert path) until the
build completes. Concurrent build matches the pattern established in
005 / 007 / 011 / 016 / 017.

Revision ID: 024
Revises: 023
Create Date: 2026-06-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "024"
down_revision: str | None = "023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_fleet_commands_node_created_deploy "
            "ON fleet_commands (node_id, created_at) "
            "WHERE command = 'deploy'"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_fleet_commands_node_created_deploy")
