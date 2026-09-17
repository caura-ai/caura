import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Float, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from common.constants import VECTOR_DIM
from common.models.base import Base


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    fleet_id: Mapped[str | None] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    attributes: Mapped[dict | None] = mapped_column(JSONB)
    name_embedding = mapped_column(Vector(VECTOR_DIM))
    search_vector = mapped_column(TSVECTOR)


class Relation(Base):
    __tablename__ = "relations"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    fleet_id: Mapped[str | None] = mapped_column(Text)
    from_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    relation_type: Mapped[str] = mapped_column(Text, nullable=False)
    to_entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    weight: Mapped[float] = mapped_column(Float, server_default=text("1.0"))
    evidence_memory_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("memories.id", ondelete="SET NULL")
    )

    __table_args__ = (
        Index("ix_relations_from", "from_entity_id"),
        Index("ix_relations_to", "to_entity_id"),
        # For DELETEs on ``memories``, not reads here — the referencing side of
        # a SET NULL FK. Partial because the RI check never looks for NULL.
        # See migration 035.
        Index(
            "ix_relations_evidence_memory",
            "evidence_memory_id",
            postgresql_where=text("evidence_memory_id IS NOT NULL"),
        ),
    )


# Who created a ``memory_entity_links`` row. Not a free-form string: one
# predicate deletes by it (``_delete_entity_artifacts`` on the reset path) and
# three service methods write it, so a typo at any writer would silently make
# that writer's links undeletable — or deletable — with nothing failing.
LINK_SOURCE_CALLER = "caller"
LINK_SOURCE_EXTRACTION = "extraction"


class MemoryEntityLink(Base):
    __tablename__ = "memory_entity_links"

    memory_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    # Provenance, and the reason a content edit can clear the graph without
    # destroying links a caller curated. ``entity_links`` on
    # ``PATCH /memories/{id}`` is a caller-owned additive API — a way to tag a
    # memory with a project or person its text never names — and extraction,
    # which mines text, will never recreate such a link. So the edit-time reset
    # deletes only ``extraction`` rows; see migration 048 for why the default is
    # the conservative ``caller`` rather than the more accurate ``extraction``.
    source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text(f"'{LINK_SOURCE_CALLER}'")
    )

    __table_args__ = (
        # The PK ``(memory_id, entity_id)`` covers the memories-side FK on its
        # leading column, but a btree cannot serve ``entity_id`` as a prefix —
        # so deleting an entity scanned this whole table, across every tenant.
        # See migration 035.
        Index("ix_memory_entity_links_entity_id", "entity_id"),
        # The reset's delete predicate. Partial because that is the only query
        # that reads ``source``. See migration 048.
        Index(
            "ix_memory_entity_links_extraction",
            "memory_id",
            postgresql_where=text(f"source = '{LINK_SOURCE_EXTRACTION}'"),
        ),
    )
