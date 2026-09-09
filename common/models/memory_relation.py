"""Typed memory-to-memory relations — the store behind the ``memory_link`` tool.

Distinct from the two link tables that already exist, and it is worth being explicit
about why none of them could serve this:

* ``relations`` is entity <-> entity.
* ``memory_entity_links`` is memory <-> entity.
* ``memories.supersedes_id`` is memory -> memory but SINGLE-VALUED, and is written by
  the contradiction detector.

``memory_link`` promises a typed relation between two MEMORY ids, which is none of
those, and it was accepted-but-unadvertised until this table existed (memclawd #130).

WHY ``supersedes`` IS NOT STORED HERE. Eldad's call (2026-08-13): ``supersedes``
reuses ``memories.supersedes_id`` rather than becoming a row here, so the contradiction
detector and the API write one field instead of two stores that can disagree. The
consequence is a real asymmetry the tool has to document: ``supersedes`` is 1:1 and a
second one OVERWRITES, while a second ``elaborates`` is an additional row. The CHECK
constraint below makes that structural rather than a convention — a ``supersedes`` row
cannot be written here at all.

WHY ONE DIRECTED ROW, never two. Three of the five stored types are semantically
symmetric (``contradicts``, ``alternative_to``, ``related_to``), and the tempting
implementation is to write both directions. That creates pairs which must stay in sync,
and deleting one leaves a half-edge. So exactly one row is stored, exactly as the caller
stated it, and symmetry is a READ concern: queries for a symmetric type match
``from_memory_id = X OR to_memory_id = X``. All per-type knowledge lives in one constant
(``SYMMETRIC_RELATION_TYPES``) instead of being spread into the schema.

SOFT DELETE leaves rows here untouched. ``memories`` is soft-deleted routinely and that
is reversible, so cascading on soft-delete would make un-delete lossy. A link whose
endpoint is soft-deleted simply stops being returned, because the read path already
joins ``memories`` to fetch endpoint content and can add ``deleted_at IS NULL`` for
free. Terminal cleanup needs no new machinery: the purge sweep hard-deletes the row and
``ON DELETE CASCADE`` takes the links with it.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from common.models.base import Base

# The six types in ``contracts/mcp-tools.md`` §8, as specified — Eldad confirmed the set
# rather than letting it be inferred from the tool description.
#
# ``supersedes`` is routed to ``memories.supersedes_id`` and is deliberately NOT in
# ``STORED_RELATION_TYPES``; it appears here only so the tool's input schema and this
# module agree on the vocabulary.
SUPERSEDES_RELATION_TYPE = "supersedes"

# Symmetric in meaning: if A contradicts B then B contradicts A. Stored one-directionally
# anyway (see module docstring); this set is what the read path and the idempotency check
# consult. Changing membership is the entire cost of revisiting that decision.
SYMMETRIC_RELATION_TYPES = frozenset({"contradicts", "alternative_to", "related_to"})

# Asymmetric: direction carries meaning, so a reversed row is a different claim.
DIRECTED_RELATION_TYPES = frozenset({"elaborates", "depends_on"})

STORED_RELATION_TYPES = frozenset(SYMMETRIC_RELATION_TYPES | DIRECTED_RELATION_TYPES)

ALL_RELATION_TYPES = frozenset(STORED_RELATION_TYPES | {SUPERSEDES_RELATION_TYPE})


class MemoryRelation(Base):
    __tablename__ = "memory_relations"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False)
    fleet_id: Mapped[str | None] = mapped_column(Text)
    from_memory_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(Text, nullable=False)
    to_memory_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    # Caller-supplied, free-form, per ``mcp-tools.md`` §8's optional ``metadata``.
    # ``metadata_`` because ``metadata`` is reserved on the declarative Base — same
    # rename ``Memory`` uses, and the API exposes it as ``metadata``.
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        # One row per (tenant, from, type, to). This is what makes a repeated
        # ``memory_link`` call idempotent rather than duplicating the edge. For a
        # SYMMETRIC type the service must also check the REVERSED pair before
        # inserting, since (A, contradicts, B) and (B, contradicts, A) are the same
        # claim and this constraint would happily hold both.
        UniqueConstraint(
            "tenant_id",
            "from_memory_id",
            "relation_type",
            "to_memory_id",
            name="uq_memory_relations_natural_key",
        ),
        # Each FK's referencing column needs to be servable as an index PREFIX, and the
        # natural key above leads with ``tenant_id`` so it covers neither endpoint.
        # Without these, deleting one memory scans this table across EVERY tenant —
        # migration 035 landed after exactly that cost 15.3 s of a 15.5 s bulk delete.
        # ``tests/test_skill_schema_v1.py::test_every_fk_referencing_column_is_index_leading``
        # fails at PR time if either is dropped.
        Index("ix_memory_relations_from", "from_memory_id"),
        Index("ix_memory_relations_to", "to_memory_id"),
        # Tenant-scoped listing, and the leading column for any future tenant sweep.
        Index("ix_memory_relations_tenant", "tenant_id"),
        # ``supersedes`` lives on ``memories.supersedes_id``; admitting it here would
        # create a second, disagreeing source of truth for the same claim. Enforced in
        # the schema so the routing cannot be bypassed by a direct insert.
        CheckConstraint(
            "relation_type <> 'supersedes'",
            name="ck_memory_relations_supersedes_not_stored",
        ),
        # A memory relating to itself is meaningless for all five stored types, and is
        # the shape a caller passing the same id twice produces by accident.
        CheckConstraint(
            "from_memory_id <> to_memory_id",
            name="ck_memory_relations_no_self_link",
        ),
    )
