"""Typed memory-to-memory relations — the store behind the ``memory_link`` tool.

``memory_link`` promises a typed relation between two MEMORY ids, and nothing in the
schema could hold one: ``relations`` is entity<->entity, ``memory_entity_links`` is
memory<->entity, and ``memories.supersedes_id`` is memory->memory but single-valued and
written only by the contradiction detector. The tool was therefore accepted but
unadvertised (memclawd #130) until this table existed.

Design decisions this encodes, so a reader does not have to reconstruct them:

* **``supersedes`` is NOT stored here.** It reuses ``memories.supersedes_id`` so the
  detector and the API write one field rather than two stores that can disagree. The
  CHECK below makes that structural — a ``supersedes`` row cannot be inserted at all.
* **One directed row per link, never two.** Three of the five stored types are
  semantically symmetric, but storing both directions creates pairs that must stay in
  sync and half-edges when one is deleted. Symmetry is handled on READ.
* **Soft delete leaves rows here.** ``memories`` is soft-deleted routinely and that is
  reversible; the read path filters on ``deleted_at IS NULL`` instead. ``ON DELETE
  CASCADE`` handles the terminal case when the purge sweep hard-deletes the row.

No backfill: there is no existing memory<->memory edge data to migrate. The
contradiction detector's ``supersedes_id`` pointers stay exactly where they are.

Indexes are plain, not CONCURRENTLY: the table is created in this same migration, so it
is empty and unlocked — the CONCURRENTLY requirement (and
``test_no_plain_create_index_on_large_tables``) applies to indexes added to large
PRE-EXISTING tables, which is what crashed six storage-writer boots on 2026-06-16.

Revision ID: 037
Revises: 036
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "037"
down_revision: str | None = "036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_relations",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False
        ),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("fleet_id", sa.Text(), nullable=True),
        sa.Column("from_memory_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("relation_type", sa.Text(), nullable=False),
        sa.Column("to_memory_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["from_memory_id"], ["memories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_memory_id"], ["memories.id"], ondelete="CASCADE"),
        # Makes a repeated ``memory_link`` call idempotent instead of duplicating the
        # edge. For a SYMMETRIC type the service must ALSO check the reversed pair
        # before inserting — (A, contradicts, B) and (B, contradicts, A) are the same
        # claim, and this constraint would happily hold both.
        sa.UniqueConstraint(
            "tenant_id",
            "from_memory_id",
            "relation_type",
            "to_memory_id",
            name="uq_memory_relations_natural_key",
        ),
        sa.CheckConstraint(
            "relation_type <> 'supersedes'",
            name="ck_memory_relations_supersedes_not_stored",
        ),
        sa.CheckConstraint(
            "from_memory_id <> to_memory_id",
            name="ck_memory_relations_no_self_link",
        ),
    )
    # Both endpoints need to be servable as an index PREFIX. The natural key above
    # leads with ``tenant_id``, so it covers neither — and an unindexed FK referencing
    # column turns every parent delete into a full scan of this table across ALL
    # tenants (migration 035 landed after that cost 15.3 s of a 15.5 s bulk delete).
    op.create_index("ix_memory_relations_from", "memory_relations", ["from_memory_id"])
    op.create_index("ix_memory_relations_to", "memory_relations", ["to_memory_id"])
    op.create_index("ix_memory_relations_tenant", "memory_relations", ["tenant_id"])


def downgrade() -> None:
    # Indexes and constraints go with the table.
    op.drop_table("memory_relations")
