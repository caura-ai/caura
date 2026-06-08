"""Repository for entities, relations, and memory_entity_links queries."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.entity import Entity, MemoryEntityLink, Relation
from core_api.constants import (
    DEFAULT_RELATION_TYPE_WEIGHT,
    ENTITY_RESOLUTION_CANDIDATE_LIMIT,
    GRAPH_MAX_HOPS,
    RELATION_TYPE_WEIGHTS,
)
from core_api.repositories import scope_sql


def _relation_weight(relation_type: str, row_weight: float) -> float:
    """Compute effective weight for a relation edge.

    Combines the per-type semantic weight (from RELATION_TYPE_WEIGHTS)
    with the per-row weight stored in the DB (default 1.0).
    """
    type_w = RELATION_TYPE_WEIGHTS.get(
        relation_type.lower(),
        DEFAULT_RELATION_TYPE_WEIGHT,
    )
    return type_w * row_weight


class EntityRepository:
    """Single point of DB access for Entity, Relation, and MemoryEntityLink rows."""

    # ------------------------------------------------------------------
    # Entity CRUD
    # ------------------------------------------------------------------

    async def get_by_id(
        self,
        db: AsyncSession,
        entity_id: UUID,
    ) -> Entity | None:
        return await db.get(Entity, entity_id)

    async def find_exact(
        self,
        db: AsyncSession,
        tenant_id: str,
        entity_type: str,
        canonical_name: str,
        fleet_id: str | None = None,
    ) -> Entity | None:
        """Phase 1 entity resolution: exact match on tenant + fleet + type + name."""
        stmt = select(Entity).where(
            Entity.tenant_id == tenant_id,
            Entity.entity_type == entity_type,
            Entity.canonical_name == canonical_name,
        )
        if fleet_id:
            stmt = stmt.where(Entity.fleet_id == fleet_id)
        else:
            stmt = stmt.where(Entity.fleet_id.is_(None))

        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def find_by_embedding_similarity(
        self,
        db: AsyncSession,
        tenant_id: str,
        entity_type: str,
        name_embedding: list[float],
        fleet_id: str | None = None,
        limit: int = ENTITY_RESOLUTION_CANDIDATE_LIMIT,
    ) -> list[tuple[Entity, float]]:
        """Phase 2 entity resolution: embedding cosine similarity.

        Returns list of (Entity, similarity_score) ordered by distance.
        """
        distance = Entity.name_embedding.cosine_distance(name_embedding)
        similarity = (1.0 - distance).label("similarity")
        stmt = (
            select(Entity, similarity)
            .where(
                Entity.tenant_id == tenant_id,
                Entity.entity_type == entity_type,
                Entity.name_embedding.isnot(None),
            )
            .order_by(distance)
            .limit(limit)
        )
        if fleet_id:
            stmt = stmt.where(Entity.fleet_id == fleet_id)
        else:
            stmt = stmt.where(Entity.fleet_id.is_(None))

        result = await db.execute(stmt)
        return list(result.all())

    async def list_entities(
        self,
        db: AsyncSession,
        tenant_id: str,
        *,
        fleet_id: str | None = None,
        entity_type: str | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> list[Entity]:
        stmt = select(Entity).where(Entity.tenant_id == tenant_id).limit(limit)
        if fleet_id:
            stmt = stmt.where(Entity.fleet_id == fleet_id)
        if entity_type:
            stmt = stmt.where(Entity.entity_type == entity_type)
        if search:
            stmt = stmt.where(Entity.canonical_name.ilike(f"%{search}%"))
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def add(self, db: AsyncSession, entity: Entity) -> None:
        db.add(entity)

    # ------------------------------------------------------------------
    # Entity FTS
    # ------------------------------------------------------------------

    async def fts_search(
        self,
        db: AsyncSession,
        tokens: list[str],
        tenant_id: str,
        fleet_ids: list[str] | None = None,
    ) -> list[UUID]:
        """Full-text search against the entity tsvector index."""
        ts_query = func.plainto_tsquery("english", " ".join(tokens))
        stmt = select(Entity.id).where(
            Entity.tenant_id == tenant_id,
            Entity.search_vector.op("@@")(ts_query),
        )
        if fleet_ids:
            stmt = stmt.where(or_(Entity.fleet_id.in_(fleet_ids), Entity.fleet_id.is_(None)))
        result = await db.execute(stmt)
        return [row[0] for row in result.all()]

    # ------------------------------------------------------------------
    # Relations
    # ------------------------------------------------------------------

    async def find_relation(
        self,
        db: AsyncSession,
        tenant_id: str,
        from_entity_id: UUID,
        relation_type: str,
        to_entity_id: UUID,
        fleet_id: str | None = None,
    ) -> Relation | None:
        stmt = select(Relation).where(
            Relation.tenant_id == tenant_id,
            Relation.from_entity_id == from_entity_id,
            Relation.relation_type == relation_type,
            Relation.to_entity_id == to_entity_id,
        )
        if fleet_id:
            stmt = stmt.where(Relation.fleet_id == fleet_id)
        else:
            stmt = stmt.where(Relation.fleet_id.is_(None))

        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def add_relation(self, db: AsyncSession, relation: Relation) -> None:
        db.add(relation)

    async def list_relations(
        self,
        db: AsyncSession,
        tenant_id: str,
        *,
        fleet_id: str | None = None,
        include_null_fleet: bool = False,
    ) -> list[Relation]:
        stmt = select(Relation).where(Relation.tenant_id == tenant_id)
        if fleet_id:
            if include_null_fleet:
                stmt = stmt.where(or_(Relation.fleet_id == fleet_id, Relation.fleet_id.is_(None)))
            else:
                stmt = stmt.where(Relation.fleet_id == fleet_id)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_outgoing_relations(
        self,
        db: AsyncSession,
        entity_id: UUID,
        tenant_id: str,
    ) -> list[tuple[Relation, Entity]]:
        """Return outgoing relations with their target entities."""
        stmt = (
            select(Relation, Entity)
            .join(Entity, Entity.id == Relation.to_entity_id)
            .where(
                Relation.from_entity_id == entity_id,
                Relation.tenant_id == tenant_id,
            )
        )
        result = await db.execute(stmt)
        return list(result.all())

    # ------------------------------------------------------------------
    # Graph expansion
    # ------------------------------------------------------------------

    async def expand_graph(
        self,
        db: AsyncSession,
        seed_entity_ids: list[UUID],
        tenant_id: str,
        fleet_id: str | None,
        max_hops: int = GRAPH_MAX_HOPS,
        use_union: bool = False,
    ) -> dict[UUID, tuple[int, float]]:
        """Traverse relations from seed entities up to max_hops.

        Returns {entity_id: (min_hop_distance, relation_weight)} for all
        reachable entities (including seeds at hop 0, weight 1.0).
        """
        entity_hops: dict[UUID, tuple[int, float]] = dict.fromkeys(seed_entity_ids, (0, 1.0))
        frontier = set(seed_entity_ids)

        for hop in range(1, max_hops + 1):
            if not frontier:
                break
            fwd = select(
                Relation.to_entity_id,
                Relation.relation_type,
                Relation.weight,
            ).where(
                Relation.tenant_id == tenant_id,
                Relation.from_entity_id.in_(frontier),
            )
            rev = select(
                Relation.from_entity_id,
                Relation.relation_type,
                Relation.weight,
            ).where(
                Relation.tenant_id == tenant_id,
                Relation.to_entity_id.in_(frontier),
            )
            if fleet_id:
                fwd = fwd.where(or_(Relation.fleet_id == fleet_id, Relation.fleet_id.is_(None)))
                rev = rev.where(or_(Relation.fleet_id == fleet_id, Relation.fleet_id.is_(None)))

            if use_union:
                combined = fwd.union_all(rev)
                result = await db.execute(combined)
                all_rows = result.all()
            else:
                fwd_result = await db.execute(fwd)
                rev_result = await db.execute(rev)
                all_rows = (*fwd_result.all(), *rev_result.all())

            neighbor_weights: dict[UUID, float] = {}
            for eid, rel_type, row_w in all_rows:
                w = _relation_weight(rel_type, row_w)
                if eid not in neighbor_weights or w > neighbor_weights[eid]:
                    neighbor_weights[eid] = w

            for eid, w in neighbor_weights.items():
                if eid not in entity_hops:
                    entity_hops[eid] = (hop, w)
            frontier = neighbor_weights.keys() - {eid for eid in entity_hops if entity_hops[eid][0] < hop}

        return entity_hops

    async def get_full_graph(
        self,
        db: AsyncSession,
        tenant_id: str,
        fleet_id: str | None = None,
    ) -> tuple[list[Entity], list[Relation]]:
        """Return all entities and relations for a tenant (optionally filtered by fleet)."""
        entity_stmt = select(Entity).where(Entity.tenant_id == tenant_id)
        if fleet_id:
            entity_stmt = entity_stmt.where(or_(Entity.fleet_id == fleet_id, Entity.fleet_id.is_(None)))
        entities_result = await db.execute(entity_stmt)
        entities = list(entities_result.scalars().all())

        relation_stmt = select(Relation).where(Relation.tenant_id == tenant_id)
        if fleet_id:
            relation_stmt = relation_stmt.where(
                or_(Relation.fleet_id == fleet_id, Relation.fleet_id.is_(None))
            )
        relations_result = await db.execute(relation_stmt)
        relations = list(relations_result.scalars().all())

        return entities, relations

    # ------------------------------------------------------------------
    # Memory-entity links
    # ------------------------------------------------------------------

    async def count_memories_per_entity(
        self,
        db: AsyncSession,
        entity_ids: list[UUID],
    ) -> dict[UUID, int]:
        """Return {entity_id: count} for the given entity IDs."""
        if not entity_ids:
            return {}
        result = await db.execute(
            select(MemoryEntityLink.entity_id, func.count())
            .where(MemoryEntityLink.entity_id.in_(entity_ids))
            .group_by(MemoryEntityLink.entity_id)
        )
        return dict(result.all())

    async def get_linked_memories(
        self,
        db: AsyncSession,
        entity_id: UUID,
        tenant_id: str,
    ) -> list:
        """Return (MemoryEntityLink, Memory) rows for an entity, excluding deleted memories."""
        from common.models.memory import Memory

        stmt = (
            select(MemoryEntityLink, Memory)
            .join(Memory, Memory.id == MemoryEntityLink.memory_id)
            .where(
                MemoryEntityLink.entity_id == entity_id,
                Memory.deleted_at.is_(None),
                Memory.tenant_id == tenant_id,
            )
        )
        result = await db.execute(stmt)
        return list(result.all())

    async def get_entity_links_for_memories(
        self,
        db: AsyncSession,
        memory_ids: list[UUID],
    ) -> list[MemoryEntityLink]:
        """Return all MemoryEntityLink rows for the given memory IDs."""
        if not memory_ids:
            return []
        result = await db.execute(select(MemoryEntityLink).where(MemoryEntityLink.memory_id.in_(memory_ids)))
        return list(result.scalars().all())

    async def get_memory_ids_by_entity_ids(
        self,
        db: AsyncSession,
        entity_ids: list[UUID],
    ) -> list[tuple[UUID, UUID, str]]:
        """Return (memory_id, entity_id, role) tuples for the given entity IDs."""
        if not entity_ids:
            return []
        stmt = select(
            MemoryEntityLink.memory_id,
            MemoryEntityLink.entity_id,
            MemoryEntityLink.role,
        ).where(MemoryEntityLink.entity_id.in_(entity_ids))
        result = await db.execute(stmt)
        return list(result.all())

    async def find_entity_link(
        self,
        db: AsyncSession,
        memory_id: UUID,
        entity_id: UUID,
    ) -> MemoryEntityLink | None:
        result = await db.execute(
            select(MemoryEntityLink).where(
                MemoryEntityLink.memory_id == memory_id,
                MemoryEntityLink.entity_id == entity_id,
            )
        )
        return result.scalar_one_or_none()

    async def add_entity_link(
        self,
        db: AsyncSession,
        link: MemoryEntityLink,
    ) -> None:
        db.add(link)

    async def delete_entity_links(
        self,
        db: AsyncSession,
        links: list[MemoryEntityLink],
    ) -> None:
        for link in links:
            await db.delete(link)

    # ------------------------------------------------------------------
    # Crystallizer helpers
    # ------------------------------------------------------------------

    async def find_orphaned_entities(
        self,
        db: AsyncSession,
        tenant_id: str,
        fleet_id: str | None,
        limit: int = 100,
    ) -> list[tuple]:
        """Entities with zero memory_entity_links. Returns (id, canonical_name) tuples."""
        scope, params = scope_sql(tenant_id, fleet_id, table="e")
        result = await db.execute(
            text(f"""
            SELECT e.id, e.canonical_name
            FROM entities e
            LEFT JOIN memory_entity_links mel ON mel.entity_id = e.id
            WHERE {scope}
              AND mel.entity_id IS NULL
            LIMIT :lim
        """),
            {**params, "lim": limit},
        )
        return list(result.all())

    async def find_broken_entity_links(
        self,
        db: AsyncSession,
        tenant_id: str,
        fleet_id: str | None,
        limit: int = 100,
    ) -> list[tuple]:
        """Entity links pointing to soft-deleted memories. Returns (memory_id, entity_id) tuples."""
        scope, params = scope_sql(tenant_id, fleet_id)
        result = await db.execute(
            text(f"""
            SELECT mel.memory_id, mel.entity_id
            FROM memory_entity_links mel
            JOIN memories m ON m.id = mel.memory_id
            WHERE {scope}
              AND m.deleted_at IS NOT NULL
            LIMIT :lim
        """),
            {**params, "lim": limit},
        )
        return list(result.all())
