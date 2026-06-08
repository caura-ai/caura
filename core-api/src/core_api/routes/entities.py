import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.constants import DEFAULT_ENTITY_LIMIT, MAX_LIST_LIMIT
from core_api.db.session import get_db
from core_api.schemas import (
    EntityOut,
    EntityUpsert,
    RelationUpsert,
    RelationUpsertOut,
)
from core_api.services.audit_service import log_cross_tenant_read
from core_api.services.entity_service import get_entity, upsert_entity, upsert_relation
from core_api.services.usage_service import check_and_increment_by_tenant as check_and_increment

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Knowledge Graph"])


@router.get("/entities")
async def list_entities(
    tenant_id: str = Query(...),
    fleet_id: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_ENTITY_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """List all entities for a tenant.

    Reads widen across the caller's ``readable_tenant_ids`` set when the
    requested ``tenant_id`` is in that set — the same contract memory
    reads use (see ``routes/memories.py:list_memories``). Cross-tenant
    reads emit a ``cross_tenant_read`` audit event TO the source tenant
    so per-tenant audit-log queries surface "who read FROM my tenant".
    """
    auth.enforce_readable_tenant(tenant_id)
    sc = get_storage_client()
    entities = await sc.list_entities(tenant_id, fleet_id=fleet_id, limit=limit)

    # Count linked memories per entity
    eids = [e.get("id", "") for e in entities]
    memory_counts_raw = await sc.count_memories_per_entity(tenant_id, eids) if eids else {}

    if auth.is_cross_tenant_read and tenant_id != auth.tenant_id:
        await log_cross_tenant_read(
            db,
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


@router.get("/graph")
async def get_graph(
    tenant_id: str = Query(...),
    fleet_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Return full knowledge graph (entities + relations) for a tenant.

    Matches the read-widening contract used by memory reads — cross-tenant
    credentials may inspect the graph of any tenant in
    ``readable_tenant_ids``. Audited to the source tenant.
    """
    auth.enforce_readable_tenant(tenant_id)

    sc = get_storage_client()
    graph = await sc.get_full_graph(tenant_id, fleet_id)

    entities = graph.get("entities", [])
    relations = graph.get("relations", [])

    logger.info(
        f"Graph query: tenant={tenant_id} fleet={fleet_id} → {len(entities)} entities, {len(relations)} relations"
    )

    if auth.is_cross_tenant_read and tenant_id != auth.tenant_id:
        await log_cross_tenant_read(
            db,
            home_tenant_id=auth.tenant_id,
            home_agent_id=auth.agent_id,
            source_tenants=[tenant_id],
            surface="rest_graph",
            result_count_by_tenant={tenant_id: len(entities) + len(relations)},
        )

    # Memory counts per entity
    eids = [e.get("id", "") for e in entities]
    memory_counts_raw = await sc.count_memories_per_entity(tenant_id, eids) if eids else {}

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
    db: AsyncSession = Depends(get_db),
):
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    auth.enforce_tenant(body.tenant_id)
    if auth.tenant_id:
        await check_and_increment(db, body.tenant_id, "write")
    # NOTE: entity upsert uses its own connection (storage-api HTTP
    # client); not atomic with the ``check_and_increment`` quota
    # bump above. ``db`` is intentionally dropped at the call site so
    # the non-atomicity is visible at the seam rather than hidden
    # inside ``upsert_entity`` (where the param was historically
    # accepted-and-ignored).
    return await upsert_entity(data=body)


@router.get("/entities/{entity_id}", response_model=EntityOut)
async def get_entity_route(
    entity_id: UUID,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Fetch a single entity. Mirrors ``GET /memories/{memory_id}`` —
    reads widen via ``readable_tenant_ids``; foreign-tenant reads are
    audited to the source tenant."""
    auth.enforce_readable_tenant(tenant_id)
    entity = await get_entity(db, entity_id, tenant_id, caller_agent_id=auth.agent_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Entity not found")
    if auth.is_cross_tenant_read and tenant_id != auth.tenant_id:
        await log_cross_tenant_read(
            db,
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
    db: AsyncSession = Depends(get_db),
):
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    auth.enforce_tenant(body.tenant_id)
    if auth.tenant_id:
        await check_and_increment(db, body.tenant_id, "write")
    return await upsert_relation(db, body)
