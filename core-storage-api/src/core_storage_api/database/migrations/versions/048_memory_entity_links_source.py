"""Record who created each ``memory_entity_links`` row: a caller, or extraction.

Editing a memory's content re-runs entity extraction, and extraction only ever
ADDS -- ``memory_add_entity_links`` upserts ``ON CONFLICT DO NOTHING`` and
nothing removes -- so a row edited from "Alice joined Acme" to "Bob joined
Globex" kept Alice and Acme, linked and still ranking the row in recall for
names its content no longer contains. Clearing the graph on a content edit is
what fixes that, and it is what ``memory_reset_entity_artifacts`` does.

Clearing ALL of a row's links is too much, though, and this column is what makes
the difference expressible. ``entity_links`` on ``PATCH /memories/{id}`` is a
caller-owned, additive public API: a way to tag a memory with a project or a
person its text never literally names. Extraction mines text, so it will never
recreate such a link. Without a provenance marker, a reset destroys it
permanently on the next content edit -- and the caller gets no signal, because
that PATCH need not mention ``entity_links`` at all.
``tests/test_entity_links_are_additive.py`` already ruled on exactly this shape:
a shipped endpoint must not silently DELETE links a caller did not name.
Reaching that outcome through a different code path is the same change.

The three writers are already distinct methods, so nothing has to be inferred:
``memory_add_entity_links`` (PATCH ``entity_links``) and ``entity_create_link``
(``POST /entities/links``) are callers; ``entity_bulk_upsert_links`` is the
extraction worker.

DEFAULT ``'caller'``, which is the conservative direction and deliberately not
the common one. Rows already in the table predate this column and cannot be
attributed -- most of them ARE extraction's -- so defaulting to 'extraction'
would be the accurate guess. It would also make the first content edit after
this deploy delete every caller-curated link that exists today, which is the one
irreversible outcome available here.

'caller' under-deletes instead, and the cost is worth stating plainly: a row that
predates this column keeps that default for good. ``entity_bulk_upsert_links``
does not put ``source`` in its ON CONFLICT SET, so extraction re-mining the same
entity does not take the row over -- which is what protects a genuine caller link
from being reclassified, and equally what leaves a genuine extraction link
sticky. So the edit-time reset governs links created from here on; the stale ones
already in the table stay until something else removes them. Under-deleting is
recoverable, over-deleting another caller's rows is not -- the same principle
``_delete_entity_artifacts`` states for its candidate set -- and an operator
backfill can reclassify later against extraction output if that is ever wanted.

NOT NULL with a server default is metadata-only in PostgreSQL 11+: the default
is stored in the catalog and existing rows are not rewritten.

Revision ID: 048
Revises: 047
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "048"
down_revision: str | None = "047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept in sync with ``common.models.entity``; the values are written by three
# service methods and read by one delete predicate. Inlined rather than imported
# because a migration must keep describing the schema at ITS revision even after
# the model moves on.
_CALLER = "caller"
_EXTRACTION = "extraction"
_INDEX_NAME = "ix_memory_entity_links_extraction"


def upgrade() -> None:
    op.add_column(
        "memory_entity_links",
        sa.Column(
            "source",
            sa.Text(),
            nullable=False,
            server_default=sa.text(f"'{_CALLER}'"),
        ),
    )
    # Partial, on the only predicate that reads this column: the reset deletes
    # ``source = 'extraction'`` for one memory. The composite PK already leads
    # on ``memory_id``, so this exists to keep that delete from re-checking
    # every link of a heavily-linked memory, not to find the memory.
    #
    # CONCURRENTLY, in an autocommit block: ``memory_entity_links`` is one of
    # the large tables, and a plain CREATE INDEX blocks writes for the whole
    # build while holding the migration advisory lock -- which crashed
    # storage-writer boots on 2026-06-16. Same shape as 026 and 046, and the
    # repo's own ratchet (``test_no_plain_create_index_on_large_tables``)
    # enforces it.
    with op.get_context().autocommit_block():
        connection = op.get_context().connection
        if connection is None:
            raise RuntimeError("online migration requires a connection")
        # An interrupted CONCURRENTLY build leaves an invalid index behind. It
        # still costs writes and never serves reads, and ``IF NOT EXISTS`` would
        # keep it, so drop it first and rebuild. Same guard as 041 / 046.
        invalid = connection.execute(
            sa.text(
                """
                SELECT 1 FROM pg_index i
                JOIN pg_class c ON c.oid = i.indexrelid
                WHERE c.relname = :name
                  AND NOT i.indisvalid
                """
            ),
            {"name": _INDEX_NAME},
        ).scalar()
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} "
            f"ON memory_entity_links (memory_id) WHERE source = '{_EXTRACTION}'"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}")
    op.drop_column("memory_entity_links", "source")
