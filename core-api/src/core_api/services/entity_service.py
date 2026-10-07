import logging
from uuid import UUID

import httpx
from fastapi import HTTPException

from core_api.clients.storage_client import get_storage_client
from core_api.constants import (
    ENTITY_RESOLUTION_THRESHOLD,
)
from core_api.errors import AUTH_FLEET_SCOPE_FORBIDDEN, coded_detail
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


class AmbiguousEntityName(Exception):
    """An untyped exact lookup found the name under more than one entity type."""


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
        # Merge into the existing entity. Storage merges under a row lock
        # (L-46): this sends only what the upsert adds, never a copy of the row
        # it read, which used to PATCH back over any key another writer added
        # since. A key named here takes its value, every other key stays, and
        # ``_aliases`` is the union.
        #
        # First-seen wins (A5 #3): the stored canonical name stays, and storage
        # no longer rewrites it. The previous "promote longer name as canonical"
        # rule actively turned hallucinated suffixes into the canonical row —
        # e.g., the LLM returns ``globex industries`` for content that says only
        # ``Globex``, embedding similarity merges the two, and the canonical
        # permanently becomes ``globex industries``. Cross-link discovery then
        # surfaces false overlaps against every other ``Globex`` mention.
        # Alternative surface forms are still preserved via ``_aliases`` so they
        # remain searchable / discoverable.
        added = dict(data.attributes or {})
        aliases = list(added.get("_aliases") or [])
        for name in (entity.get("canonical_name") or "", data.canonical_name):
            if name and name not in aliases:
                aliases.append(name)
        added["_aliases"] = aliases
        # ``None`` when the row vanished since the lookup: create it below.
        entity = await sc.merge_entity(str(entity.get("id")), data.tenant_id, added, name_embedding)
    if not entity:
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

    Matches case-insensitively, as the entity natural key does, and with no
    ``entity_type`` matches any type (M-25): the caller has a name and nothing
    else, and extraction never writes the old default type. It looks only in
    ``fleet_id`` (M-119, owner decision 2026-10-06), where extraction resolves
    the same name: a fleet write that took a tenant-shared entity as subject
    would hold a different row from every later mention, which extraction links
    to the fleet's own. A fleet-less write looks among tenant-shared entities.
    Raises :class:`AmbiguousEntityName` when the name belongs to more than one
    type, so the caller can skip rather than guess.
    """
    sc = get_storage_client()
    try:
        row = await sc.find_exact_entity(
            tenant_id=tenant_id,
            name=canonical_name,
            fleet_id=fleet_id or None,
            entity_type=entity_type,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 409:
            raise AmbiguousEntityName(canonical_name) from exc
        raise
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


async def entity_reader_scope(
    tenant_id: str,
    caller_agent_id: str | None,
    caller_tenant_id: str | None,
) -> dict | None:
    """The agent reader scope storage applies to ``/entities`` and ``/graph``.

    An entity name is mined from memory text and ``memory_count`` counts the
    memories behind it, so for an agent credential both follow the memory
    read contract: storage drops entities whose linked memories are all ones
    the agent may not read (link-less entities stay), and counts only the
    readable memories. This resolves that contract's
    two inputs once — the identity (paired with its home tenant, since agent
    ids are unique per tenant) and the cross-fleet trust ladder of
    :func:`~core_api.services.agent_service.memory_access_allowed_for_agent`:
    ``caller_fleet_ids`` is ``None`` when the agent may cross fleets (trust
    >= 2, or an unregistered identity, mirroring that helper's allow-on-
    unknown), otherwise the fleets its ``scope_team`` reads are confined to.

    ``None`` for tenant / user / admin credentials: those keep the
    tenant-wide listing, as ``get_entity`` and the relation filter do, less
    what storage hides from every reader: entities mined only from
    soft-deleted memories, and edges with no live evidence (M-92).
    """
    if not caller_agent_id:
        return None
    from core_api.services.agent_service import lookup_agent

    agent = await lookup_agent(tenant_id, caller_agent_id)
    return _reader_for(agent, caller_agent_id, caller_tenant_id)


def _reader_for(agent: dict | None, caller_agent_id: str, caller_tenant_id: str | None) -> dict:
    """:func:`entity_reader_scope` from an agent row the caller already holds."""
    fleets: list[str] | None = None
    if agent and agent.get("trust_level", 0) < 2:
        fleets = [agent["fleet_id"]] if agent.get("fleet_id") else []
    return {
        "caller_agent_id": caller_agent_id,
        "caller_tenant_id": caller_tenant_id,
        "caller_fleet_ids": fleets,
    }


async def filter_relations_by_evidence_visibility(
    relations: list[dict],
    *,
    tenant_id: str,
    caller_agent_id: str | None,
    caller_tenant_id: str | None,
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
        caller_tenant_id: the caller's HOME tenant, which ``tenant_id`` is not
            on a pinned read of a sibling. A sibling's ``scope_agent`` evidence
            written under the caller's agent name is not the caller's (M-94).
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
            row_tenant_id=tenant_id,
            caller_tenant_id=caller_tenant_id,
        )

    return [rel for rel in relations if _visible(rel)]


async def get_entity(
    entity_id: UUID, tenant_id: str, caller_agent_id: str | None = None, *, caller_tenant_id: str | None
) -> EntityOut | None:
    """``caller_tenant_id`` is the caller's HOME tenant, for the ``scope_agent``
    pairing on the linked memories and relations (M-94); required, so no caller
    can leave it out.

    For an agent, an entity :func:`entity_reader_scope` hides from its lists is
    ``None``, as a missing id is (M-83)."""
    from core_api.services.agent_service import lookup_agent, memory_access_allowed_for_agent

    sc = get_storage_client()
    # Resolve the caller's agent row ONCE: it scopes the entity read, the
    # linked-memory filter below and the relation filter after it.
    caller_agent: dict | None = None
    reader: dict | None = None
    if caller_agent_id:
        caller_agent = await lookup_agent(tenant_id, caller_agent_id)
        reader = _reader_for(caller_agent, caller_agent_id, caller_tenant_id)
    result = await sc.get_entity_with_linked_memories(str(entity_id), tenant_id, reader)
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
    if caller_agent_id:
        linked_memories_raw = [
            mem
            for mem in linked_memories_raw
            if memory_access_allowed_for_agent(
                caller_agent,
                caller_agent_id,
                visibility=mem.get("visibility"),
                owner_agent_id=mem.get("agent_id"),
                fleet_id=mem.get("fleet_id"),
                row_tenant_id=tenant_id,
                caller_tenant_id=caller_tenant_id,
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
        caller_tenant_id=caller_tenant_id,
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


async def upsert_relation(data: RelationUpsert, *, fleet_scope: dict | None = None) -> RelationUpsertOut:
    """``fleet_scope`` (M-84): ``{"fleet_id": ...}`` for an agent credential below
    trust 3, so storage refuses a relation whose endpoints, evidence or existing
    edge are outside that fleet. ``None`` for every other caller."""
    sc = get_storage_client()

    # Storage API does an actual UPSERT (``ON CONFLICT DO UPDATE`` on
    # ``uq_relations_natural_key``) — duplicate-relation IntegrityErrors
    # are silently absorbed and the existing row's weight + evidence
    # are refreshed to the new values.
    try:
        relation = await sc.create_relation(
            {
                "tenant_id": data.tenant_id,
                "fleet_id": data.fleet_id,
                "from_entity_id": str(data.from_entity_id),
                "relation_type": data.relation_type,
                "to_entity_id": str(data.to_entity_id),
                "weight": data.weight,
                "evidence_memory_id": str(data.evidence_memory_id) if data.evidence_memory_id else None,
                **({"fleet_scope": fleet_scope} if fleet_scope is not None else {}),
            }
        )
    except httpx.HTTPStatusError as exc:
        # Storage's fleet check refuses with 403, and runs only when this call
        # sent a ``fleet_scope``. Any other 403 is unexpected and propagates,
        # as other upstream statuses do (M-42).
        if exc.response.status_code == 403 and fleet_scope is not None:
            raise HTTPException(
                status_code=403,
                detail=coded_detail(
                    AUTH_FLEET_SCOPE_FORBIDDEN,
                    "fleet-scope policy: the relation reaches a fleet this agent may not write.",
                ),
            ) from exc
        if exc.response.status_code != 409:
            raise
        # Storage refuses foreign and missing endpoints identically. Preserve
        # that boundary without leaking upstream details or returning a 500.
        raise HTTPException(
            status_code=422,
            detail="from_entity_id or to_entity_id does not exist in this tenant",
        ) from exc

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
