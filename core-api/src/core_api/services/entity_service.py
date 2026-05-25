import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from core_api.clients.storage_client import get_storage_client
from core_api.constants import (
    ENTITY_RESOLUTION_THRESHOLD,
)
from core_api.schemas import (
    EntityLinkOut,
    EntityOut,
    EntityUpsert,
    MemoryOut,
    RelationOut,
    RelationUpsert,
    RelationUpsertOut,
)
from core_api.services.hooks import get_hooks

logger = logging.getLogger(__name__)


async def upsert_entity(
    data: EntityUpsert,
    *,
    name_embedding: list[float] | None = None,
) -> EntityOut:
    """Two-phase entity upsert with optional embedding-based resolution.

    Signature: ``data`` is required and positional; ``name_embedding``
    is keyword-only. The function uses the storage client (HTTP) for
    all writes — there is no AsyncSession parameter because the
    operation is NOT transactional with the caller's DB session.
    Routes that combine ``check_and_increment`` with this call must
    document the non-atomicity at the seam (see
    ``routes/entities.py``); the function itself can't enforce it.

    Phase 1: Exact match on tenant + fleet + canonical_name (fast, btree).
    Phase 2: If no exact match AND name_embedding is provided, check for
             similar entities of the same type via cosine similarity.
    """
    sc = get_storage_client()

    # Phase 1: exact match (fast path)
    entity = await sc.find_exact_entity(
        data.tenant_id,
        data.canonical_name,
        data.fleet_id,
        entity_type=data.entity_type,
    )

    # Phase 2: embedding similarity (only if Phase 1 found nothing)
    if entity is None and name_embedding is not None:
        rows = await sc.find_by_embedding_similarity(
            data.tenant_id,
            name_embedding,
            limit=3,
            entity_type=data.entity_type,
            fleet_id=data.fleet_id,
        )
        for row in rows:
            sim = row.get("similarity", 0.0)
            if sim >= ENTITY_RESOLUTION_THRESHOLD:
                entity = row.get("entity") or row
                logger.info(
                    "Entity resolution: '%s' matched '%s' (sim=%.3f)",
                    data.canonical_name,
                    entity.get("canonical_name"),
                    sim,
                )
                break

    if entity:
        # Merge into existing entity via storage client update
        entity_id = entity.get("id")
        existing_attrs = entity.get("attributes") or {}
        merged_attrs = dict(existing_attrs)
        if data.attributes:
            merged_attrs.update(data.attributes)

        # Track alias in attributes
        aliases = list(merged_attrs.get("_aliases", []))
        existing_name = entity.get("canonical_name", "")
        if existing_name not in aliases:
            aliases.append(existing_name)
        if data.canonical_name not in aliases:
            aliases.append(data.canonical_name)
        merged_attrs["_aliases"] = aliases

        # First-seen wins (A5 #3). The previous "promote longer name as
        # canonical" rule actively turned hallucinated suffixes into the
        # canonical row — e.g., the LLM returns ``globex industries`` for
        # content that says only ``Globex``, embedding similarity merges
        # the two, and the canonical permanently becomes ``globex
        # industries``. Cross-link discovery then surfaces false overlaps
        # against every other ``Globex`` mention. Alternative surface
        # forms are still preserved via the ``_aliases`` list above so
        # they remain searchable / discoverable.
        new_canonical = existing_name

        update_data: dict = {
            "entity_type": data.entity_type,
            "canonical_name": new_canonical,
            "attributes": merged_attrs,
        }
        if name_embedding is not None:
            update_data["name_embedding"] = name_embedding

        updated = await sc.update_entity(str(entity_id), update_data)
        entity = updated or entity
    else:
        # Create new entity. Coerce ``attributes=None`` to ``{}`` so
        # the persisted row matches what the update branch (line ~80)
        # already does on its merge — ``None``-typed JSONB columns
        # would otherwise diverge from update-branch's ``{}`` and
        # confuse downstream readers that expect a dict.
        create_data: dict = {
            "tenant_id": data.tenant_id,
            "fleet_id": data.fleet_id,
            "entity_type": data.entity_type,
            "canonical_name": data.canonical_name,
            "attributes": data.attributes or {},
        }
        if name_embedding is not None:
            create_data["name_embedding"] = name_embedding
        entity = await sc.create_entity(create_data)

    return EntityOut(
        id=entity.get("id"),
        tenant_id=entity.get("tenant_id"),
        fleet_id=entity.get("fleet_id"),
        entity_type=entity.get("entity_type"),
        canonical_name=entity.get("canonical_name"),
        attributes=entity.get("attributes"),
    )


async def get_entity(db: AsyncSession, entity_id: UUID, tenant_id: str) -> EntityOut | None:
    sc = get_storage_client()
    result = await sc.get_entity_with_linked_memories(str(entity_id))
    if not result:
        return None

    entity = result.get("entity", {})
    if entity.get("tenant_id") != tenant_id:
        return None

    # Build linked memories from dict data
    raw_entries = result.get("linked_memories", [])
    linked_memories_raw = [entry.get("memory", entry) for entry in raw_entries]
    linked_memories = []
    for mem in linked_memories_raw:
        entity_links_raw = mem.get("entity_links", [])
        entity_links = [
            EntityLinkOut(entity_id=el.get("entity_id"), role=el.get("role")) for el in entity_links_raw
        ]
        # See ``memory_service._dict_to_memory_out`` for the
        # falsy-``{}`` trap.
        raw_meta = mem.get("metadata_")
        metadata = raw_meta if raw_meta is not None else mem.get("metadata")
        linked_memories.append(
            MemoryOut(
                id=mem.get("id"),
                tenant_id=mem.get("tenant_id"),
                fleet_id=mem.get("fleet_id"),
                agent_id=mem.get("agent_id"),
                memory_type=mem.get("memory_type"),
                content=mem.get("content"),
                weight=mem.get("weight"),
                source_uri=mem.get("source_uri"),
                run_id=mem.get("run_id"),
                metadata=metadata,
                created_at=mem.get("created_at"),
                expires_at=mem.get("expires_at"),
                entity_links=entity_links,
                recall_count=mem.get("recall_count"),
                last_recalled_at=mem.get("last_recalled_at"),
            )
        )

    # Outgoing relations
    relations_raw = entity.get("relations", [])
    relations = [
        RelationOut(
            id=rel.get("id"),
            relation_type=rel.get("relation_type"),
            to_entity_id=rel.get("to_entity_id"),
            to_entity_name=rel.get("to_entity_name"),
            weight=rel.get("weight"),
            evidence_memory_id=rel.get("evidence_memory_id"),
        )
        for rel in relations_raw
    ]

    # Increment recall_count for linked memories via hooks
    memory_ids = [mem.get("id") for mem in linked_memories_raw if mem.get("id")]
    _hooks = get_hooks()
    if memory_ids and _hooks.on_recall:
        try:
            # on_recall hook still expects db; pass it through for now
            await _hooks.on_recall(db, memory_ids)
        except Exception:
            pass  # Non-critical

    return EntityOut(
        id=entity.get("id"),
        tenant_id=entity.get("tenant_id"),
        fleet_id=entity.get("fleet_id"),
        entity_type=entity.get("entity_type"),
        canonical_name=entity.get("canonical_name"),
        attributes=entity.get("attributes"),
        linked_memories=linked_memories,
        relations=relations,
    )


async def upsert_relation(db: AsyncSession, data: RelationUpsert) -> RelationUpsertOut:
    sc = get_storage_client()

    # Storage API does an actual UPSERT (``ON CONFLICT DO UPDATE`` on
    # ``uq_relations_natural_key``) — duplicate-relation IntegrityErrors
    # are silently absorbed and the existing row's weight + evidence
    # are refreshed to the new values.
    relation = await sc.create_relation(
        {
            "tenant_id": data.tenant_id,
            "fleet_id": data.fleet_id,
            "from_entity_id": str(data.from_entity_id),
            "relation_type": data.relation_type,
            "to_entity_id": str(data.to_entity_id),
            "weight": data.weight,
            "evidence_memory_id": str(data.evidence_memory_id) if data.evidence_memory_id else None,
        }
    )

    return RelationUpsertOut(
        id=relation.get("id"),
        tenant_id=relation.get("tenant_id"),
        fleet_id=relation.get("fleet_id"),
        from_entity_id=relation.get("from_entity_id"),
        relation_type=relation.get("relation_type"),
        to_entity_id=relation.get("to_entity_id"),
        weight=relation.get("weight"),
        evidence_memory_id=relation.get("evidence_memory_id"),
    )
