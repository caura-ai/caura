import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from core_api import openapi_responses as _oar
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.constants import DEFAULT_ENTITY_LIMIT, MAX_LIST_LIMIT
from core_api.errors import AUTH_AGENT_TRUST_TOO_LOW, coded_detail
from core_api.schemas import (
    EntityOut,
    EntityUpsert,
    RelationUpsert,
    RelationUpsertOut,
)
from core_api.services.agent_service import enforce_fleet_write
from core_api.services.audit_service import log_cross_tenant_read
from core_api.services.entity_service import (
    entity_reader_scope,
    filter_relations_by_evidence_visibility,
    get_entity,
    upsert_entity,
    upsert_relation,
)
from core_api.services.usage_service import check_and_increment_by_tenant as check_and_increment

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Knowledge Graph"])


async def _graph_write_scope(body: EntityUpsert | RelationUpsert, auth: AuthContext) -> dict | None:
    """The memory write's gates for a graph write by an agent credential (M-84).

    As on ``POST /documents``: another fleet needs trust >= 3 (this also
    registers the agent on first contact), an agent awaiting approval (trust 0)
    is refused, and an omitted ``fleet_id`` becomes the agent's home fleet (a
    fleet-less node or edge is read by every fleet). Returns the fleet a
    relation's endpoints, evidence and existing edge must stay in, for storage
    to check, or ``None`` when the caller may reach any fleet: a tenant
    credential, or an agent at trust >= 3.
    """
    if not (auth.tenant_id and auth.agent_id):
        return None
    agent = await enforce_fleet_write(body.tenant_id, auth.agent_id, body.fleet_id)
    if agent.get("trust_level", 0) == 0:
        raise HTTPException(
            status_code=403,
            detail=coded_detail(
                AUTH_AGENT_TRUST_TOO_LOW,
                f"Agent '{auth.agent_id}' is not approved. Contact tenant admin to set trust_level >= 1.",
            ),
        )
    if not body.fleet_id and agent.get("fleet_id"):
        body.fleet_id = agent["fleet_id"]
    if agent.get("trust_level", 0) >= 3:
        return None
    return {"fleet_id": agent.get("fleet_id")}


@router.get("/entities", responses={200: {"model": list[_oar.EntityListItem]}})
async def list_entities(
    tenant_id: str = Query(...),
    fleet_id: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_ENTITY_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
):
    """List all entities for a tenant, a page at a time: ``limit`` rows from
    ``offset`` (L-22; without it, nothing past the first page was reachable).

    Reads widen across the caller's ``readable_tenant_ids`` set when the
    requested ``tenant_id`` is in that set — the same contract memory
    reads use (see ``routes/memories.py:list_memories``). Cross-tenant
    reads emit a ``cross_tenant_read`` audit event TO the source tenant
    so per-tenant audit-log queries surface "who read FROM my tenant".

    An agent credential is not shown an entity whose linked memories are all
    ones it may not read, and ``memory_count`` counts only the memories it may
    read — the same contract ``GET /entities/{id}`` applies to the memories it
    returns. An entity with no linked memories at all (a manual upsert) is
    listed, with ``memory_count`` 0: it derives from no memory.
    """
    auth.enforce_readable_tenant(tenant_id)
    sc = get_storage_client()
    # C22 — ``search`` and ``entity_type`` were declared on this route since
    # day one but never forwarded, so ``GET /entities?search=foo`` silently
    # returned the unfiltered list. The storage service always supported both
    # (entity_list: ilike on canonical_name, equality on entity_type).
    #
    # An agent credential does not see entities mined only from memories it
    # may not read, and ``memory_count`` covers the readable ones alone: the
    # name is mined from memory text, and ``GET /entities/{id}`` already hides
    # the memories behind it. Link-less entities stay visible. Tenant / user /
    # admin credentials keep the full list, less entities mined only from
    # soft-deleted memories, which storage hides from every reader (M-92).
    reader = await entity_reader_scope(tenant_id, auth.agent_id, auth.tenant_id)
    entities = await sc.list_entities(
        tenant_id,
        fleet_id=fleet_id,
        entity_type=entity_type,
        search=search,
        limit=limit,
        offset=offset,
        reader=reader,
    )

    # Count linked memories per entity
    eids = [e.get("id", "") for e in entities]
    memory_counts_raw = await sc.count_memories_per_entity(tenant_id, eids, reader=reader) if eids else {}

    if auth.is_cross_tenant_read and tenant_id != auth.tenant_id:
        await log_cross_tenant_read(
            home_tenant_id=auth.tenant_id,
            home_agent_id=auth.agent_id,
            source_tenants=[tenant_id],
            surface="rest_entities_list",
            result_count_by_tenant={tenant_id: len(entities)},
        )

    return [
        {
            "id": str(e.get("id", "")),
            "tenant_id": e.get("tenant_id"),
            "fleet_id": e.get("fleet_id"),
            "entity_type": e.get("entity_type"),
            "canonical_name": e.get("canonical_name"),
            "attributes": e.get("attributes"),
            "memory_count": memory_counts_raw.get(str(e.get("id", "")), 0),
        }
        for e in entities
    ]


@router.get("/graph", responses={200: {"model": _oar.GraphResponse}})
async def get_graph(
    tenant_id: str = Query(...),
    fleet_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
):
    """Return full knowledge graph (entities + relations) for a tenant.

    Matches the read-widening contract used by memory reads — cross-tenant
    credentials may inspect the graph of any tenant in
    ``readable_tenant_ids``. Audited to the source tenant.

    For an agent credential, nodes and ``memory_count`` follow ``GET
    /entities`` (hidden only when every linked memory is one the agent may
    not read), and an edge
    is returned only when both its endpoints and its evidence memory are
    visible to it.
    """
    auth.enforce_readable_tenant(tenant_id)

    sc = get_storage_client()
    # Nodes and ``memory_count`` follow the same agent reader scope as
    # ``GET /entities`` (see there); storage also drops edges to hidden nodes.
    reader = await entity_reader_scope(tenant_id, auth.agent_id, auth.tenant_id)
    graph = await sc.get_full_graph(tenant_id, fleet_id, reader=reader)

    entities = graph.get("entities", [])
    relations = graph.get("relations", [])

    # Evidence-visibility filter, shared with ``GET /entities/{id}`` (audit
    # H-03). The sibling has always dropped edges whose evidence memory the
    # caller cannot read; this route returned every edge for the tenant —
    # ``relation_type``, both endpoints, and ``evidence_memory_id`` verbatim —
    # behind nothing but ``enforce_readable_tenant``. So an agent could read
    # here exactly what the sibling refused it: the triple derived from a
    # peer's ``scope_agent`` memory, plus that memory's id. ``fleet_id`` is
    # optional, so omitting it asked for every fleet at once.
    #
    # The raw memory text was never exposed either way. The derived triple IS
    # the leak — "(anna) -negotiates-> (zenith acquisition)" carries the
    # secret without carrying the sentence.
    #
    # No-op for tenant / user / admin credentials (``agent_id`` None), matching
    # the sibling's contract rather than narrowing this route for dashboards.
    #
    # Runs BEFORE the log line and the cross-tenant audit below, so both count
    # what was actually disclosed. Counting pre-filter would overstate the
    # disclosure and record a cross-tenant read of edges that were withheld.
    relations = await filter_relations_by_evidence_visibility(
        relations,
        tenant_id=tenant_id,
        caller_agent_id=auth.agent_id,
        caller_tenant_id=auth.tenant_id,
    )

    logger.info(
        f"Graph query: tenant={tenant_id} fleet={fleet_id} → {len(entities)} entities, {len(relations)} relations"
    )

    if auth.is_cross_tenant_read and tenant_id != auth.tenant_id:
        await log_cross_tenant_read(
            home_tenant_id=auth.tenant_id,
            home_agent_id=auth.agent_id,
            source_tenants=[tenant_id],
            surface="rest_graph",
            result_count_by_tenant={tenant_id: len(entities) + len(relations)},
        )

    # Memory counts per entity
    eids = [e.get("id", "") for e in entities]
    memory_counts_raw = await sc.count_memories_per_entity(tenant_id, eids, reader=reader) if eids else {}

    nodes = [
        {
            "id": str(e.get("id", "")),
            "label": e.get("canonical_name"),
            "type": e.get("entity_type"),
            "fleet_id": e.get("fleet_id"),
            "attributes": e.get("attributes"),
            "memory_count": memory_counts_raw.get(str(e.get("id", "")), 0),
        }
        for e in entities
    ]

    edges = [
        {
            "id": str(r.get("id", "")),
            "source": str(r.get("from_entity_id", "")),
            "target": str(r.get("to_entity_id", "")),
            "relation_type": r.get("relation_type"),
            "weight": float(r.get("weight", 0)),
            "evidence_memory_id": str(r.get("evidence_memory_id")) if r.get("evidence_memory_id") else None,
        }
        for r in relations
    ]

    return JSONResponse({"nodes": nodes, "edges": edges})


@router.post("/entities/upsert", response_model=EntityOut, status_code=200)
async def upsert_entity_route(
    body: EntityUpsert,
    auth: AuthContext = Depends(get_auth_context),
):
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    auth.enforce_tenant(body.tenant_id)
    # The memory write's gates. Tenant keys carry no trust level and are
    # unaffected.
    await _graph_write_scope(body, auth)
    if auth.tenant_id:
        await check_and_increment(body.tenant_id, "write")
    # NOTE: entity upsert uses its own connection (storage-api HTTP
    # client); not atomic with the ``check_and_increment`` quota
    # bump above. ``db`` is intentionally dropped at the call site so
    # the non-atomicity is visible at the seam rather than hidden
    # inside ``upsert_entity`` (where the param was historically
    # accepted-and-ignored).
    entity = await upsert_entity(data=body)
    # M-83 (owner decision 2026-10-05): an upsert that lands on an entity hidden
    # from this agent still merges, but answers with only what it sent, not the
    # stored attributes and ``_aliases`` behind a name it guessed. That hides
    # what the entity holds, not that it exists: the write lands in it by
    # design, and the answer carries its id, which a by-id read then answers
    # 404. The check reads back the row just written, so it goes to the
    # writer: a lagging replica would read a visible entity as hidden.
    reader = await entity_reader_scope(body.tenant_id, auth.agent_id, auth.tenant_id)
    if reader and not await get_storage_client().get_entity(
        str(entity.id), body.tenant_id, reader, read=False
    ):
        return EntityOut(
            id=entity.id,
            tenant_id=body.tenant_id,
            fleet_id=body.fleet_id,
            entity_type=body.entity_type,
            canonical_name=body.canonical_name,
            attributes=body.attributes or {},
        )
    return entity


@router.get("/entities/{entity_id}", response_model=EntityOut)
async def get_entity_route(
    entity_id: UUID,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
):
    """Fetch a single entity. Mirrors ``GET /memories/{memory_id}`` —
    reads widen via ``readable_tenant_ids``; foreign-tenant reads are
    audited to the source tenant."""
    auth.enforce_readable_tenant(tenant_id)
    entity = await get_entity(
        entity_id, tenant_id, caller_agent_id=auth.agent_id, caller_tenant_id=auth.tenant_id
    )
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    if auth.is_cross_tenant_read and tenant_id != auth.tenant_id:
        await log_cross_tenant_read(
            home_tenant_id=auth.tenant_id,
            home_agent_id=auth.agent_id,
            source_tenants=[tenant_id],
            surface="rest_entity_get",
            result_count_by_tenant={tenant_id: 1},
        )
    return entity


@router.post("/relations/upsert", response_model=RelationUpsertOut, status_code=200)
async def upsert_relation_route(
    body: RelationUpsert,
    auth: AuthContext = Depends(get_auth_context),
):
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    auth.enforce_tenant(body.tenant_id)
    # As on ``POST /entities/upsert`` above, and storage holds the relation's
    # endpoints, evidence and existing edge to the same fleet.
    fleet_scope = await _graph_write_scope(body, auth)
    if auth.tenant_id:
        await check_and_increment(body.tenant_id, "write")
    return await upsert_relation(body, fleet_scope=fleet_scope)
