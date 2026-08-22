"""``documents.created_at`` / ``updated_at`` NOT NULL.

Both columns were created with ``server_default=now()`` but WITHOUT
``nullable=False`` (001_initial_schema, never tightened since), so a NULL has
always been representable even though every insert path sets them. OSS #826 made
the API honest about that — ``DocOut`` required a ``datetime``, so a NULL row
raised ValidationError inside the read route and 500'd it, across six call sites
including three list endpoints. This closes the hole at the source, which is where
it belongs: the columns describe row lifecycle, not optional user data.

BACKFILL FIRST, because ``SET NOT NULL`` on a table containing a NULL fails — and
migrations here run in the FastAPI lifespan startup hook, so a failed one means the
new revision never becomes ready and the deploy stalls (Cloud Run keeps the old
revision serving; it fails safe, but it fails).

The backfill derives from the row where it can: a NULL ``created_at`` takes
``updated_at``, which is an upper bound on it, and vice versa. When BOTH are NULL
there is nothing to derive from — ``id`` is ``gen_random_uuid()`` (v4), so it
carries no time — and ``now()`` is the only available value. That asserts the row
was created at migration time, which is false; it is chosen because the alternative
is refusing to migrate. Such rows are identifiable afterwards by sharing a
near-identical timestamp with each other and with this migration's run.

LOCK SAFETY. ``documents`` is one of this repo's declared large tables (see
``test_no_plain_create_index_on_large_tables``), and a bare ``SET NOT NULL`` takes
an AccessExclusive lock while it full-scans to verify — blocking writes for the
duration and holding the migration advisory lock, which is the shape that crashed
6 storage-writer boots on 2026-06-16 (migration 025). Instead:

    ADD CONSTRAINT ... CHECK (col IS NOT NULL) NOT VALID   -- AccessExclusive, O(1)
    VALIDATE CONSTRAINT ...                                -- ShareUpdateExclusive,
                                                           -- scans WITHOUT blocking
                                                           -- reads or writes
    ALTER COLUMN ... SET NOT NULL                          -- AccessExclusive, but
                                                           -- PG12+ skips the scan
                                                           -- given the valid CHECK
    DROP CONSTRAINT ...                                    -- now redundant

Each step is its own transaction (``autocommit_block``), which is the whole point:
run them inside one and the AccessExclusive from step 1 is held across the scan in
step 2, reproducing exactly the blocking this pattern exists to avoid.

Revision ID: 038
Revises: 037
Create Date: 2026-08-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "038"
down_revision: str | None = "037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("created_at", "updated_at")


def upgrade() -> None:
    # Row locks on the NULL rows only, so this stays in the normal transaction.
    # ``created_at`` first: a both-NULL row then takes ``now()`` here and the same
    # value below, rather than two timestamps a few milliseconds apart.
    op.execute(
        "UPDATE documents SET created_at = COALESCE(created_at, updated_at, now()) WHERE created_at IS NULL"
    )
    op.execute(
        "UPDATE documents SET updated_at = COALESCE(updated_at, created_at, now()) WHERE updated_at IS NULL"
    )

    # ``autocommit_block`` commits the backfill above before the DDL runs, which is
    # required: VALIDATE must see the rows as committed, and each ALTER needs its
    # own transaction so no lock spans the scan.
    with op.get_context().autocommit_block():
        for column in _COLUMNS:
            constraint = f"ck_documents_{column}_not_null"
            # Postgres has no ADD CONSTRAINT IF NOT EXISTS, and a migration that
            # fails after the ADD would otherwise be unretryable — same
            # re-runnability rationale as 037's DROP INDEX ... IF EXISTS.
            op.execute(f"ALTER TABLE documents DROP CONSTRAINT IF EXISTS {constraint}")
            op.execute(
                f"ALTER TABLE documents ADD CONSTRAINT {constraint} CHECK ({column} IS NOT NULL) NOT VALID"
            )
            op.execute(f"ALTER TABLE documents VALIDATE CONSTRAINT {constraint}")
            op.execute(f"ALTER TABLE documents ALTER COLUMN {column} SET NOT NULL")
            op.execute(f"ALTER TABLE documents DROP CONSTRAINT {constraint}")


def downgrade() -> None:
    # Metadata-only; dropping a NOT NULL needs no scan, so no autocommit dance.
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE documents ALTER COLUMN {column} DROP NOT NULL")
