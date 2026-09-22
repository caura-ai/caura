import logging
from uuid import UUID

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

        updated = await sc.update_entity(str(entity_id), data.tenant_id, update_data)
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


async def find_entity_by_exact_name(
    *,
    tenant_id: str,
    canonical_name: str,
    fleet_id: str | None = None,
    entity_type: str | None = None,
) -> UUID | None:
    """Resolve a name to an EXISTING entity, or None. Never creates.

    The lookup-only counterpart to ``upsert_entity``'s Phase 1, split out
    because one caller needs the resolution WITHOUT the fallback create:
    ``EmitMemoryTriple``'s proper-noun subject path. For an identifier-shaped
    subject ("TOKEN-XYZ") creating the row on a miss is safe, because the
    string itself is the canonical name and entity extraction would later
    emit the same one. For a proper noun ("Alice", "Atlas") it is not — the
    name alone does not determine ``entity_type``, so a create here would
    race the extraction worker's higher-precision row and fragment the
    entity. Resolving against what already exists keeps the precision of
    that worker while still filling the subject column on repeat mentions.

    Returns the id rather than an ``EntityOut``: the only caller needs the
    foreign key, and returning less keeps this from drifting into a second
    read path for entity content.
    """
    sc = get_storage_client()
    row = await sc.find_exact_entity(
        tenant_id=tenant_id,
        name=canonical_name,
        fleet_id=fleet_id,
        entity_type=entity_type,
    )
    if not row:
        return None
    raw = row.get("id")
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, AttributeError, TypeError):
        # A malformed id from storage must not break the write pipeline;
        # the caller treats None as "unresolved" and skips the triple.
        logger.warning("find_entity_by_exact_name: unparseable entity id %r", raw)
        return None


async def filter_relations_by_evidence_visibility(
    relations: list[dict],
    *,
    tenant_id: str,
    caller_agent_id: str | None,
    caller_agent: dict | None = None,
    pre_authorized_memory_ids: set[str] | None = None,
) -> list[dict]:
    """Drop relation edges whose evidence memory the caller may not read.

    The scope contract (audit S5 / C14): an agent credential must not
    enumerate relation edges or ``evidence_memory_id``s pointing at memories
    it cannot read. The raw memory text stays unreadable either way — the leak
    is the memory-DERIVED triple plus the private memory's id.

    Shared deliberately. This filter used to live inside :func:`get_entity`,
    which made the guarantee a property of ONE caller — and ``GET /graph``,
    reading the same edges from a different storage call, had no filter at all
    (audit H-03). The comment in ``get_entity`` about its tenant check already
    names that defect class: a guarantee that lives in one caller is not a
    guarantee. Route both readers through here so a third one cannot be added
    without it.

    ``caller_agent_id`` of ``None`` means a tenant / user / admin credential:
    the list is returned unchanged, matching the linked-memory filter's
    contract in the same module.

    Args:
        relations: dicts carrying ``evidence_memory_id`` (other keys ignored).
        caller_agent: the caller's pre-fetched agent row, when the calling code
            already resolved it. Resolved here once if not supplied — never
            per-edge, which would be N+1 over the graph.
        pre_authorized_memory_ids: ids the caller has already been shown by
            the same response (``get_entity``'s linked memories), so their
            edges need no second lookup.
    """
    if not caller_agent_id or not relations:
        return relations

    from core_api.services.agent_service import (
        lookup_agent,
        memory_access_allowed_for_agent,
    )

    sc = get_storage_client()
    if caller_agent is None:
        caller_agent = await lookup_agent(tenant_id, caller_agent_id)

    authorized_ids = pre_authorized_memory_ids or set()
    unknown_evidence_ids = list(
        dict.fromkeys(
            str(rel["evidence_memory_id"])
            for rel in relations
            if rel.get("evidence_memory_id") and str(rel["evidence_memory_id"]) not in authorized_ids
        )
    )
    evidence_rows: dict[str, dict | None] = {}
    if unknown_evidence_ids:
        # One bulk round-trip (not per-relation); missing / cross-tenant ids
        # come back as None in-slot.
        fetched = await sc.bulk_get_memories(unknown_evidence_ids, tenant_id=tenant_id)
        evidence_rows = dict(zip(unknown_evidence_ids, fetched))

    def _visible(rel: dict) -> bool:
        evidence_id = rel.get("evidence_memory_id")
        if not evidence_id:
            # No evidence ⇒ no memory-derived content and no id to leak.
            return True
        evidence_id = str(evidence_id)
        if evidence_id in authorized_ids:
            return True
        row = evidence_rows.get(evidence_id)
        if row is None:
            # Deleted / nonexistent / cross-tenant evidence — don't leak the
            # edge or the memory id. Fails CLOSED: an unresolvable evidence id
            # is the one case where we cannot show the caller may read it.
            return False
        return memory_access_allowed_for_agent(
            caller_agent,
            caller_agent_id,
            visibility=row.get("visibility"),
            owner_agent_id=row.get("agent_id"),
            fleet_id=row.get("fleet_id"),
        )

    return [rel for rel in relations if _visible(rel)]


async def get_entity(entity_id: UUID, tenant_id: str, caller_agent_id: str | None = None) -> EntityOut | None:
    sc = get_storage_client()
    result = await sc.get_entity_with_linked_memories(str(entity_id), tenant_id)
    if not result:
        return None

    entity = result.get("entity", {})
    # Kept although storage now applies the same predicate. This comparison used
    # to be the ONLY thing scoping this read — the defect class being that the
    # guarantee lived in one caller — and the two fail independently: this one
    # covers a storage regression, the predicate covers any other caller.
    if entity.get("tenant_id") != tenant_id:
        return None

    # Build linked memories from dict data
    raw_entries = result.get("linked_memories", [])
    linked_memories_raw = [entry.get("memory", entry) for entry in raw_entries]

    # Fleet/agent-scope filter: an agent credential must only see the linked
    # memories it may read (same scope_agent + cross-fleet trust contract as
    # GET /memories/{id} and search). Without this the entity is a side-door
    # that returns a peer agent's scope_agent secret / cross-fleet content by
    # entity id. No-op for tenant/user/admin credentials (caller_agent_id None).
    caller_agent: dict | None = None
    if caller_agent_id:
        from core_api.services.agent_service import (
            lookup_agent,
            memory_access_allowed_for_agent,
        )

        # Resolve the caller's agent row ONCE — the per-memory loop used to
        # issue an identical lookup_agent round-trip for every scope_team
        # row (N+1 over the entity's linked memories).
        caller_agent = await lookup_agent(tenant_id, caller_agent_id)
        linked_memories_raw = [
            mem
            for mem in linked_memories_raw
            if memory_access_allowed_for_agent(
                caller_agent,
                caller_agent_id,
                visibility=mem.get("visibility"),
                owner_agent_id=mem.get("agent_id"),
                fleet_id=mem.get("fleet_id"),
            )
        ]
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

    # Outgoing relations. C23 — since v1.0.0 this read
    # ``entity.get("relations", [])`` off the with-memories payload, which has
    # NEVER carried a relations key (ENTITY_FIELDS has none), so relations were
    # structurally always ``[]`` on REST and MCP alike while /graph showed the
    # edges — the read-surface inconsistency two field reports hit. Fetch them
    # from the storage endpoint built for exactly this (and until now unused).
    # Failure degrades to ``[]`` — the pre-C23 behavior — rather than failing
    # the whole entity read; same resilience contract as find_successors.
    #
    # Scope contract (audit S5 / C14), unchanged and now operating on real
    # rows: an agent credential must not enumerate relation edges or
    # evidence_memory_ids pointing at memories it cannot read. A relation is
    # visible iff its evidence memory is readable by the caller (relations
    # with no evidence carry no memory-derived content and stay).
    try:
        relation_rows = await sc.get_outgoing_relations(str(entity_id), tenant_id=tenant_id)
    except Exception:
        logger.warning(
            "get_outgoing_relations failed for entity %s; returning entity without relations",
            entity_id,
            exc_info=True,
        )
        relation_rows = []
    relations_raw = []
    for row in relation_rows:
        rel = row.get("relation", {}) or {}
        target = row.get("target", {}) or {}
        relations_raw.append(
            {
                "id": rel.get("id"),
                "relation_type": rel.get("relation_type"),
                "to_entity_id": rel.get("to_entity_id"),
                "to_entity_name": target.get("canonical_name"),
                "weight": rel.get("weight"),
                "evidence_memory_id": rel.get("evidence_memory_id"),
            }
        )
    # Shared with ``GET /graph`` — see
    # :func:`filter_relations_by_evidence_visibility`. ``caller_agent`` is
    # passed through because the linked-memory filter above already resolved
    # it; the helper would otherwise repeat the lookup.
    relations_raw = await filter_relations_by_evidence_visibility(
        relations_raw,
        tenant_id=tenant_id,
        caller_agent_id=caller_agent_id,
        caller_agent=caller_agent,
        pre_authorized_memory_ids={str(mem.get("id")) for mem in linked_memories_raw if mem.get("id")},
    )
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

    # Increment recall_count for linked memories via storage.
    memory_ids = [mem.get("id") for mem in linked_memories_raw if mem.get("id")]
    if memory_ids:
        try:
            await get_storage_client().increment_recall(
                [str(m) for m in memory_ids],
                tenant_id=tenant_id,
            )
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


async def upsert_relation(data: RelationUpsert) -> RelationUpsertOut:
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


async def bulk_upsert_relations(data: list[RelationUpsert]) -> list[bool]:
    """Upsert many relations in ONE storage round-trip.

    Returns a list aligned to ``data``: ``True`` where the relation landed,
    ``False`` where storage refused that item (``error="fk_violation"`` — an
    endpoint that does not exist, or is not in this tenant).

    Per-item outcomes, not a single verdict, so the caller keeps the failure
    isolation the serial loop gave it: ``entity_extraction_worker`` guards every
    relation individually because one failed upsert used to take out the A65
    predicate write-back and the ``Trigger.ENTITY`` fire for that memory
    (#1495). Batching the ROUND-TRIP is the win; batching the OUTCOME would
    hand that regression back.

    Every item must carry the same ``tenant_id``: storage binds one tenant per
    request so a batch cannot span namespaces. ``ValueError`` if they disagree,
    rather than silently writing them all under the first one's tenant.
    """
    if not data:
        return []

    tenants = {d.tenant_id for d in data}
    if len(tenants) != 1:
        raise ValueError(f"bulk_upsert_relations requires one tenant per call, got {sorted(tenants)}")
    tenant_id = next(iter(tenants))

    results = await get_storage_client().bulk_create_relations(
        tenant_id=tenant_id,
        items=[
            {
                "input_idx": i,
                "fleet_id": d.fleet_id,
                "from_entity_id": str(d.from_entity_id),
                "relation_type": d.relation_type,
                "to_entity_id": str(d.to_entity_id),
                "weight": d.weight,
                "evidence_memory_id": str(d.evidence_memory_id) if d.evidence_memory_id else None,
            }
            for i, d in enumerate(data)
        ],
    )

    # Indexed by ``input_idx`` rather than zip'd positionally. A short or
    # reordered response is a storage-side partial failure, and reading it
    # positionally would silently attribute one relation's outcome to another —
    # the same defensive shape ``entity_extraction_worker`` already applies to
    # ``bulk_upsert_entities``. A missing slot reads as "did not land", which is
    # the direction that under-reports rather than inventing a success.
    landed = [False] * len(data)
    for r in results:
        idx = r.get("input_idx")
        if not isinstance(idx, int) or not 0 <= idx < len(data):
            logger.warning(
                "bulk_create_relations returned out-of-range input_idx %r (sent %d); skipping",
                idx,
                len(data),
            )
            continue
        landed[idx] = r.get("error") is None and r.get("relation") is not None
    return landed
