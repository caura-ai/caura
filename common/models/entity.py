import uuid

from pgvector.sqlalchemy import Vector  # type: ignore[import-not-found]
from sqlalchemy import DDL, Float, ForeignKey, Index, Text, UniqueConstraint, event, text
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


# ``search_vector`` is maintained by a Postgres trigger, not the ORM —
# ``Entity`` writes never set it directly; FTS reads (``fts_search_entities``)
# depend on it being populated. The trigger + function + GIN index are created
# by migration 001 in real deployments, but ``Base.metadata.create_all`` (used
# to build test/dev schemas) only knows ORM-declared objects and would leave
# ``search_vector`` perpetually NULL. These ``after_create`` hooks replay the
# migration-001 DDL onto any create_all-built schema so FTS behaves identically.
# Keep this in lock-step with migration 001 (entities_search_vector_update).
event.listen(
    Entity.__table__,
    "after_create",
    DDL(
        "CREATE INDEX IF NOT EXISTS ix_entities_search_vector "
        "ON entities USING GIN (search_vector)"
    ),
)
event.listen(
    Entity.__table__,
    "after_create",
    DDL(
        "CREATE OR REPLACE FUNCTION entities_search_vector_update() "
        "RETURNS trigger AS $$ BEGIN "
        "NEW.search_vector := to_tsvector('english', coalesce(NEW.canonical_name, '')); "
        "RETURN NEW; END $$ LANGUAGE plpgsql"
    ),
)
event.listen(
    Entity.__table__,
    "after_create",
    DDL(
        "CREATE TRIGGER entities_search_vector_trigger "
        "BEFORE INSERT OR UPDATE OF canonical_name ON entities "
        "FOR EACH ROW EXECUTE FUNCTION entities_search_vector_update()"
    ),
)


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
        # Natural key matching migration 001 — the target of the
        # ``ON CONFLICT ON CONSTRAINT uq_relations_natural_key`` upsert in
        # entity_service / postgres_service. Declaring it on the model keeps
        # create_all-built schemas (tests) in sync with the migration so the
        # upsert resolves the constraint by name.
        UniqueConstraint(
            "tenant_id",
            "from_entity_id",
            "relation_type",
            "to_entity_id",
            name="uq_relations_natural_key",
        ),
    )


class MemoryEntityLink(Base):
    __tablename__ = "memory_entity_links"

    memory_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True
    )
    entity_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
