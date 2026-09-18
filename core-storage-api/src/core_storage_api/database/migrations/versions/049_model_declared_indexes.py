"""Create the two indexes the models declared and no migration ever built.

08/14 L-25 + 09/02 L-14. ``Base.metadata`` and the migration chain had drifted
in both directions. Most of that is corrected in the models, because most of it
was the models over-claiming: an ``index=True`` on a column generates
SQLAlchemy's own ``ix_<table>_<column>`` name, which sat beside a differently
named ``Index(...)`` in ``__table_args__`` that the migration actually created,
so the model described two indexes where the database had one.

Two are not over-claims. Both are wanted, neither exists, and both are created
here rather than deleted from the model:

``ix_analysis_reports_tenant_started`` — ``report_get_latest_completed``
filters ``tenant_id`` (now also ``fleet_id``) and orders by ``started_at DESC``
with ``LIMIT 1``. ``ix_analysis_reports_tenant_id`` alone leaves the sort to be
done over every one of a tenant's reports. ``started_at DESC`` because that is
what the model declares (``started_at.desc()``); building it ASC instead left
alembic proposing to drop and rebuild it, which is how the direction was caught.

``ix_memory_derivations_tenant_id`` — the org hard-purge deletes from this
table by ``tenant_id`` (it was added to ``_PURGE_TENANT_TABLES`` with
``memory_conflicts``, which has had ``ix_memory_conflicts_tenant`` all along).
Without this the purge sequentially scans the table per tenant.

CONCURRENTLY for both. Neither table is in the ``large_tables`` set that
``test_no_plain_create_index_on_large_tables`` enforces, but both are populated
in any deployment old enough to need this migration, and a plain CREATE INDEX
holds an AccessExclusiveLock AND the migration advisory lock for the whole
build — the failure mode that crashed six storage-writer boots on 2026-06-16.
``IF NOT EXISTS`` keeps it idempotent across the advisory-lock-serialised
startup path.

Revision ID: 049
Revises: 048
Create Date: 2026-09-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "049"
down_revision: str | None = "048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # One source line each, so the CONCURRENTLY guard's regex sees the
        # clause rather than skipping a split string.
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_analysis_reports_tenant_started ON analysis_reports (tenant_id, started_at DESC)"
        )
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memory_derivations_tenant_id ON memory_derivations (tenant_id)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_memory_derivations_tenant_id")
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_analysis_reports_tenant_started")
