"""``ix_memories_baton``: find the memories written for a handoff baton.

Handoff batons, HB-10. A receiver links its findings back to the baton it
worked from with ``metadata.baton_id``; ``baton_id`` on ``caura_list``,
``GET /memories``, ``/search`` and ``/recall`` reads them back. The key is
``(tenant_id, metadata ->> 'baton_id')``, the expression the filter compares,
partial on rows that carry one, so it holds baton findings only: empty when this
runs, and an ordinary write never enters it.

``metadata`` is ``json``, not ``jsonb``, so the predicate is ``->> IS NOT
NULL`` (the form 058 and 062 use) rather than ``?``. Deleted rows stay in the
index: ``caura_list`` with ``include_deleted`` uses the same filter.

Built CONCURRENTLY (``memories`` is large), after clearing a half-built one from
an interrupted run (065).

Revision ID: 075
Revises: 074
Create Date: 2026-11-05
"""

from collections.abc import Sequence

from alembic import op

from core_storage_api.database.migration_helpers import drop_invalid_indexes

revision: str = "075"
down_revision: str | None = "074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        drop_invalid_indexes("ix_memories_baton")
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memories_baton ON memories "
            "(tenant_id, (metadata ->> 'baton_id')) "
            "WHERE (metadata ->> 'baton_id') IS NOT NULL"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memories_baton")
