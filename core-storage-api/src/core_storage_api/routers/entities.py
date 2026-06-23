"""Entity CRUD, graph, relation, and memory-link endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.routers._validation import _require, _require_number
from core_storage_api.schemas import (
    ENTITY_FIELDS,
    MEMORY_ENTITY_LINK_FIELDS,
    MEMORY_FIELDS,
    RELATION_FIELDS,
    orm_to_dict,
)
from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/entities", tags=["Entities"])
_svc = PostgresService()


def _validate_input_idxs(items: list[dict]) -> None:
    """Ensure each bulk-endpoint item has a unique, in-range ``input_idx``.

    The two bulk endpoints (``/bulk-upsert``, ``/bulk-resolve``) place
    their response into ``results[item["input_idx"]]``. An out-of-range
    ``input_idx`` would crash with IndexError → 500; a duplicate would
    overwrite an earlier slot and return a list shorter than the input.
    Validate both up-front so the failure mode is a clear 422 rather
    than a stack trace.
    """
    idxs: set[int] = set()
    for i, item in enumerate(items):
        raw = item.get("input_idx")
        if not isinstance(raw, int) or raw < 0 or raw >= len(items):
            raise HTTPException(
                status_code=422,
                detail=f"item {i}: input_idx must be int in [0, {len(items)})",
            )
        if raw in idxs:
            raise HTTPException(
                status_code=422,
                detail=f"item {i}: duplicate input_idx {raw}",
            )
        idxs.add(raw)


# ------------------------------------------------------------------
# Entity CRUD (collection-level)
# ------------------------------------------------------------------


@router.post("")
async def create_entity(request: Request) -> dict:
    body: dict = await request.json()
    try:
        entity = await _svc.entity_add(body)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return orm_to_dict(entity, ENTITY_FIELDS)


@router.get("")
async def list_entities(
    tenant_id: str,
    fleet_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    entities = await _svc.entity_list(tenant_id, fleet_id=fleet_id, limit=limit, offset=offset)
    return [orm_to_dict(e, ENTITY_FIELDS) for e in entities]


@router.get("/exact")
async def find_exact_entity(
    tenant_id: str,
    name: str,
    entity_type: str = "default",
    fleet_id: str | None = None,
) -> dict:
    entity = await _svc.entity_find_exact(tenant_id, entity_type, name, fleet_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return orm_to_dict(entity, ENTITY_FIELDS)


# ------------------------------------------------------------------
# FTS
# ------------------------------------------------------------------


@router.post("/fts-search")
async def fts_search_entities(request: Request) -> list[str]:
    body: dict = await request.json()
    ids = await _svc.entity_fts_search(
        tokens=body["tokens"],
        tenant_id=body["tenant_id"],
        fleet_ids=body.get("fleet_ids"),
    )
    return [str(eid) for eid in ids]


# ------------------------------------------------------------------
# Embedding similarity (entity resolution)
# ------------------------------------------------------------------


@router.post("/embedding-similarity")
async def resolve_entity_candidates(request: Request) -> list[dict]:
    body: dict = await request.json()
    results = await _svc.entity_find_by_embedding_similarity(
        tenant_id=body["tenant_id"],
        entity_type=body["entity_type"],
        name_embedding=body["name_embedding"],
        fleet_id=body.get("fleet_id"),
        limit=body.get("limit", 5),
    )
    out = []
    for entity, sim in results:
        row = orm_to_dict(entity, ENTITY_FIELDS)
        row["similarity"] = float(sim)
        out.append(row)
    return out


@router.post("/bulk-upsert")
async def bulk_upsert_entities(request: Request) -> list[dict]:
    """Apply many entity create/update operations in one round-trip.

    Companion to ``/entities/bulk-resolve`` — caller takes the resolve
    output, runs the client-side merge (first-seen-wins canonical,
    accumulate aliases), then sends the resulting create/update plan
    here.

    Per-item shape: ``{"input_idx", "action": "create"|"update",
    "entity_id"?, "tenant_id", "fleet_id", "entity_type",
    "canonical_name", "attributes", "name_embedding"?}``.

    Response is aligned to input order. ``action`` in the response
    reflects what actually happened:

    - ``"created"``: INSERT succeeded
    - ``"updated"``: UPDATE matched
    - ``"merged"``: INSERT lost a race; the row that won was updated
      with this caller's attributes (mirrors ``entity_add``'s recovery)
    - ``"missing"``: UPDATE didn't match (entity_id deleted between
      resolve and upsert)

    Cap: 500 items per request.
    """
    body: dict = await request.json()
    items = body.get("items", [])
    if not isinstance(items, list):
        raise HTTPException(status_code=422, detail="'items' must be a list")
    if len(items) > 500:
        raise HTTPException(
            status_code=422,
            detail=f"bulk-upsert capped at 500 items (got {len(items)})",
        )
    # Validate required per-item fields up-front so a missing key
    # surfaces as a 422 instead of an uncaught KeyError → 500 inside
    # the service. ``action`` is checked separately below.
    _REQUIRED_BASE = {"tenant_id", "entity_type", "canonical_name", "attributes"}
    for item in items:
        missing = _REQUIRED_BASE - item.keys()
        if missing:
            raise HTTPException(
                status_code=422,
                detail=(f"item at input_idx {item.get('input_idx')!r} missing fields: {sorted(missing)}"),
            )
    _validate_input_idxs(items)
    # Validate per-item action + update preconditions up-front. The
    # service partitions on action ∈ {"create", "update"}; unknown
    # values would otherwise be silently dropped (response shorter
    # than input), and a missing entity_id on action="update" would
    # crash inside the service with a KeyError → 500.
    for item in items:
        if item.get("action") not in {"create", "update"}:
            raise HTTPException(
                status_code=422,
                detail=(f"invalid action {item.get('action')!r} at input_idx {item.get('input_idx')!r}"),
            )
        if item.get("action") == "update":
            eid_raw = item.get("entity_id")
            if not eid_raw:
                raise HTTPException(
                    status_code=422,
                    detail=f"action='update' requires 'entity_id' at input_idx {item.get('input_idx')!r}",
                )
            # Validate UUID shape at the router boundary — without this
            # a non-UUID ``entity_id`` would crash inside the service
            # (``UUID(eid)``) and surface via the generic 500 fallback.
            try:
                UUID(eid_raw)
            except (ValueError, AttributeError):
                raise HTTPException(
                    status_code=422,
                    detail=f"invalid entity_id UUID at input_idx {item.get('input_idx')!r}",
                )
    try:
        return await _svc.entity_bulk_upsert(items)
    except (ValueError, KeyError):
        # The service no longer raises ValueError for the TOCTOU race
        # (it reports ``action="missing"`` instead). Any ValueError /
        # KeyError reaching here is an internal inconsistency, not a
        # client-resolvable conflict — 500 is the honest status. Use a
        # generic detail so internals (raw item dicts, traceback bits)
        # don't leak across the API boundary; the real cause is in the
        # server logs.
        raise HTTPException(status_code=500, detail="internal entity upsert error")


@router.post("/bulk-resolve")
async def bulk_resolve_entities(request: Request) -> list[dict | None]:
    """Resolve many entities in one round-trip using the same precedence
    as ``entity_service.upsert_entity`` (Phase 1 exact → Phase 2 cosine).

    Body shape::

        {
          "tenant_id": "...",
          "threshold": 0.85,                 # required, no server-side default
          "items": [
            {"input_idx": 0, "fleet_id": null, "canonical_name": "...",
             "entity_type": "...", "name_embedding": [...] | null},
            ...
          ]
        }

    Response is a list aligned to ``input_idx``: each element is either
    ``null`` (no match) or ``{"entity_id", "canonical_name", "attributes",
    "matched_by": "exact" | "similarity", "similarity": float}``. Callers
    use the ``matched_by`` field to decide whether to take the update path
    (with client-side attribute merge) or the create path in a follow-up
    ``/entities/bulk-upsert`` call.

    Cap: 500 items per request. Bigger batches risk pushing the Phase 1
    OR-of-ANDs plan into a seq scan; chunk client-side if you need more.
    """
    body: dict = await request.json()
    items = body.get("items", [])
    if not isinstance(items, list):
        raise HTTPException(status_code=422, detail="'items' must be a list")
    if len(items) > 500:
        raise HTTPException(
            status_code=422,
            detail=f"bulk-resolve capped at 500 items (got {len(items)})",
        )
    if "tenant_id" not in body:
        raise HTTPException(status_code=422, detail="'tenant_id' is required")
    if "threshold" not in body:
        raise HTTPException(status_code=422, detail="'threshold' is required")
    # Validate required per-item fields up-front so a missing key
    # surfaces as a 422 instead of an uncaught KeyError inside the
    # service. ``name_embedding`` is optional (skips Phase 2).
    _REQUIRED_RESOLVE = {"canonical_name", "entity_type"}
    for item in items:
        missing = _REQUIRED_RESOLVE - item.keys()
        if missing:
            raise HTTPException(
                status_code=422,
                detail=(f"item at input_idx {item.get('input_idx')!r} missing fields: {sorted(missing)}"),
            )
    _validate_input_idxs(items)

    # Numeric coercion before the service call so a non-numeric value
    # from the client surfaces as a 422 rather than an uncaught
    # ValueError/TypeError → 500.
    try:
        threshold = float(body["threshold"])
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=f"'threshold' must be numeric (got {body['threshold']!r})",
        )
    try:
        candidate_limit = int(body.get("candidate_limit", 3))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=422,
            detail=f"'candidate_limit' must be int (got {body.get('candidate_limit')!r})",
        )

    return await _svc.entity_bulk_resolve(
        tenant_id=body["tenant_id"],
        items=items,
        threshold=threshold,
        candidate_limit=candidate_limit,
    )


# ------------------------------------------------------------------
# Graph
# ------------------------------------------------------------------


@router.post("/expand-graph")
async def expand_graph(request: Request) -> dict:
    body: dict = await request.json()
    result = await _svc.entity_expand_graph(
        seed_entity_ids=[UUID(eid) for eid in body["seed_entity_ids"]],
        tenant_id=body["tenant_id"],
        fleet_id=body.get("fleet_id"),
        max_hops=body.get("max_hops", 2),
        use_union=body.get("use_union", False),
    )
    return {str(eid): {"hop": hop, "weight": weight} for eid, (hop, weight) in result.items()}


@router.get("/full-graph")
async def get_full_graph(
    tenant_id: str,
    fleet_id: str | None = None,
) -> dict:
    entities, relations = await _svc.entity_get_full_graph(tenant_id, fleet_id)
    return {
        "entities": [orm_to_dict(e, ENTITY_FIELDS) for e in entities],
        "relations": [orm_to_dict(r, RELATION_FIELDS) for r in relations],
    }


# ------------------------------------------------------------------
# Relations
# ------------------------------------------------------------------


@router.post("/relations")
async def create_relation(request: Request) -> dict:
    body: dict = await request.json()
    relation = await _svc.relation_add(body)
    return orm_to_dict(relation, RELATION_FIELDS)


@router.get("/relations/find")
async def find_relation(
    source_id: str,
    target_id: str,
    relation_type: str,
) -> dict | None:
    # Derive tenant_id from source entity (client doesn't pass it).
    source = await _svc.entity_get_by_id(UUID(source_id))
    if source is None:
        raise HTTPException(status_code=404, detail="Source entity not found")
    relation = await _svc.relation_find(
        tenant_id=source.tenant_id,
        from_entity_id=UUID(source_id),
        relation_type=relation_type,
        to_entity_id=UUID(target_id),
    )
    if relation is None:
        raise HTTPException(status_code=404, detail="Relation not found")
    return orm_to_dict(relation, RELATION_FIELDS)


# ------------------------------------------------------------------
# Memory-entity links
# ------------------------------------------------------------------


@router.post("/links")
async def create_memory_entity_link(request: Request) -> dict:
    body: dict = await request.json()
    link = await _svc.entity_add_entity_link(body)
    return orm_to_dict(link, MEMORY_ENTITY_LINK_FIELDS)


@router.post("/links/bulk")
async def bulk_upsert_memory_entity_links(request: Request) -> list[dict]:
    """Idempotently create many memory→entity links in one round-trip.

    Body: ``{"items": [{"input_idx", "memory_id", "entity_id", "role"}, ...]}``.
    Response is aligned to input order with ``{"input_idx", "memory_id",
    "entity_id", "role", "created": bool}`` — ``created=False`` means a
    row with the same ``(memory_id, entity_id)`` PK already existed and
    its prior ``role`` is preserved (matches today's find-then-create
    flow which never overwrites role).

    Cap: 500 items per request.
    """
    body: dict = await request.json()
    items = body.get("items", [])
    if not isinstance(items, list):
        raise HTTPException(status_code=422, detail="'items' must be a list")
    if len(items) > 500:
        raise HTTPException(
            status_code=422,
            detail=f"bulk-links capped at 500 items (got {len(items)})",
        )
    # Validate required per-item fields up-front so a missing key
    # surfaces as a 422 instead of an uncaught KeyError inside the
    # service.
    _REQUIRED_LINK = {"memory_id", "entity_id", "role"}
    for item in items:
        missing = _REQUIRED_LINK - item.keys()
        if missing:
            raise HTTPException(
                status_code=422,
                detail=(f"item at input_idx {item.get('input_idx')!r} missing fields: {sorted(missing)}"),
            )
    # UUID shape validation at the router boundary — without this a
    # malformed ``memory_id`` / ``entity_id`` would crash inside the
    # service's ``UUID(...)`` call and surface as an uncaught 500.
    for item in items:
        for field in ("memory_id", "entity_id"):
            try:
                UUID(item[field])
            except (ValueError, AttributeError):
                raise HTTPException(
                    status_code=422,
                    detail=f"invalid {field} UUID at input_idx {item.get('input_idx')!r}",
                )
    _validate_input_idxs(items)
    return await _svc.entity_bulk_upsert_links(items)


@router.get("/links/find")
async def find_entity_link(
    memory_id: str,
    entity_id: str,
) -> dict | None:
    link = await _svc.entity_find_entity_link(
        memory_id=UUID(memory_id),
        entity_id=UUID(entity_id),
    )
    if link is None:
        raise HTTPException(status_code=404, detail="Link not found")
    return orm_to_dict(link, MEMORY_ENTITY_LINK_FIELDS)


@router.post("/memory-ids-by-entity-ids")
async def get_memory_ids_by_entity_ids(request: Request) -> list[dict]:
    body: dict = await request.json()
    entity_ids = [UUID(eid) for eid in body["entity_ids"]]
    links = await _svc.entity_get_memory_ids_by_entity_ids(entity_ids)
    return [{"memory_id": str(mid), "entity_id": str(eid), "role": role} for mid, eid, role in links]


@router.post("/count-memories")
async def count_memories_per_entity(request: Request) -> dict:
    body: dict = await request.json()
    entity_ids = [UUID(eid) for eid in body["entity_ids"]]
    counts = await _svc.entity_count_memories_per_entity(entity_ids)
    return {str(eid): count for eid, count in counts.items()}


# ------------------------------------------------------------------
# Crystallizer / health helpers
# ------------------------------------------------------------------


@router.get("/orphaned")
async def find_orphaned_entities(tenant_id: str) -> list[dict]:
    rows = await _svc.entity_find_orphaned(tenant_id, fleet_id=None)
    return [{"id": str(row[0]), "canonical_name": row[1]} for row in rows]


@router.get("/broken-links")
async def find_broken_entity_links(tenant_id: str) -> list[dict]:
    rows = await _svc.entity_find_broken_links(tenant_id, fleet_id=None)
    return [{"memory_id": str(row[0]), "entity_id": str(row[1])} for row in rows]


# ------------------------------------------------------------------
# Entity-linking pipeline (Fix 2 Ph6) — coarse run-op endpoints that
# fold the four core-api entity-linking steps' direct DB access behind
# HTTP. Each validates its OWN contract (don't trust core-api): 422 on
# missing tenant_id / non-numeric tuning params / non-list inputs. All
# tuning constants travel in the body (storage must not import core_api).
# Placed ABOVE the parameterised ``/{entity_id}`` routes so the literal
# paths win the match.
# ------------------------------------------------------------------


@router.post("/resolve")
async def resolve_entities(request: Request) -> dict:
    """Merge duplicate entities (the full ``resolve_entities`` step) in ONE
    atomic txn with a SAVEPOINT per duplicate.

    Body ``{tenant_id, fleet_id?, batch_size, threshold, candidate_limit}``.
    Returns ``{merge_count, clusters, cluster_errors, merged_entity_ids}``."""
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    batch_size = int(_require_number(body, "batch_size"))
    threshold = _require_number(body, "threshold")
    candidate_limit = int(_require_number(body, "candidate_limit"))
    return await _svc.entity_resolve_duplicates(
        tenant_id=tenant_id,
        fleet_id=body.get("fleet_id"),
        batch_size=batch_size,
        threshold=threshold,
        candidate_limit=candidate_limit,
    )


@router.post("/discover-cross-links")
async def discover_cross_links(request: Request) -> dict:
    """Link under-connected memories to similar entities (targeted + batch),
    ONE atomic txn.

    Body ``{tenant_id, fleet_id?, batch_size, threshold, text_verify,
    target_memory_ids?}``. A non-empty ``target_memory_ids`` selects targeted
    mode. Returns ``{links_created}``. 422 on a malformed UUID in
    ``target_memory_ids`` (surfaced as a clean error instead of a 500 from the
    ``ANY(CAST(... AS uuid[]))`` cast)."""
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    batch_size = int(_require_number(body, "batch_size"))
    threshold = _require_number(body, "threshold")
    target_memory_ids = body.get("target_memory_ids")
    if target_memory_ids is not None:
        if not isinstance(target_memory_ids, list):
            raise HTTPException(status_code=422, detail="target_memory_ids (list) is required")
        try:
            for mid in target_memory_ids:
                UUID(str(mid))
        except (ValueError, AttributeError) as exc:
            raise HTTPException(status_code=422, detail=f"invalid target_memory_ids: {exc}") from exc
    return await _svc.entity_discover_cross_links(
        tenant_id=tenant_id,
        fleet_id=body.get("fleet_id"),
        batch_size=batch_size,
        threshold=threshold,
        text_verify=bool(body.get("text_verify", True)),
        target_memory_ids=target_memory_ids,
    )


@router.post("/infer-relations")
async def infer_relations(request: Request) -> dict:
    """Infer 'related_to' relations from co-occurrence (the
    ``infer_relations`` step), ONE atomic txn.

    Body ``{tenant_id, fleet_id?, batch_size, min_cooccurrence,
    reinforce_delta, max_relation_weight}``. Returns
    ``{relations_created, relations_reinforced}``."""
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    batch_size = int(_require_number(body, "batch_size"))
    min_cooccurrence = int(_require_number(body, "min_cooccurrence"))
    reinforce_delta = _require_number(body, "reinforce_delta")
    max_relation_weight = _require_number(body, "max_relation_weight")
    return await _svc.entity_infer_relations(
        tenant_id=tenant_id,
        fleet_id=body.get("fleet_id"),
        batch_size=batch_size,
        min_cooccurrence=min_cooccurrence,
        reinforce_delta=reinforce_delta,
        max_relation_weight=max_relation_weight,
    )


@router.post("/list-null-embeddings")
async def list_null_embeddings(request: Request) -> dict:
    """Entities needing a name embedding (read half of backfill).

    Body ``{tenant_id, fleet_id?, batch_size}``. Returns
    ``{rows:[{id, canonical_name}, ...]}``."""
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    batch_size = int(_require_number(body, "batch_size"))
    rows = await _svc.entity_list_null_embeddings(
        tenant_id=tenant_id,
        fleet_id=body.get("fleet_id"),
        batch_size=batch_size,
    )
    return {"rows": rows}


@router.post("/set-embeddings")
async def set_embeddings(request: Request) -> dict:
    """Write back computed name embeddings (write half of backfill), ONE
    atomic txn.

    Body ``{tenant_id, updates:[{id, embedding:[float,...]}, ...]}``. Returns
    ``{backfill_count}``. 422 on a malformed ``id`` UUID in ``updates``."""
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    updates = body.get("updates")
    if not isinstance(updates, list):
        raise HTTPException(status_code=422, detail="updates (list) is required")
    try:
        for u in updates:
            UUID(str(u["id"]))
    except (ValueError, AttributeError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid updates: {exc}") from exc
    backfill_count = await _svc.entity_set_embeddings(tenant_id=tenant_id, updates=updates)
    return {"backfill_count": backfill_count}


# ------------------------------------------------------------------
# Parameterised /{entity_id} routes — MUST stay at the bottom
# ------------------------------------------------------------------


@router.get("/{entity_id}")
async def get_entity(entity_id: UUID) -> dict:
    entity = await _svc.entity_get_by_id(entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return orm_to_dict(entity, ENTITY_FIELDS)


@router.patch("/{entity_id}")
async def update_entity(entity_id: UUID, request: Request) -> dict:
    body: dict = await request.json()
    entity = await _svc.entity_update(entity_id, body)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    return orm_to_dict(entity, ENTITY_FIELDS)


@router.get("/{entity_id}/with-memories")
async def get_entity_with_linked_memories(
    entity_id: UUID,
    tenant_id: str | None = None,
) -> dict:
    entity = await _svc.entity_get_by_id(entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    t_id = tenant_id or entity.tenant_id
    rows = await _svc.entity_get_linked_memories(entity_id, t_id)
    return {
        "entity": orm_to_dict(entity, ENTITY_FIELDS),
        "linked_memories": [
            {
                "link": orm_to_dict(link, MEMORY_ENTITY_LINK_FIELDS),
                "memory": orm_to_dict(memory, MEMORY_FIELDS),
            }
            for link, memory in rows
        ],
    }


@router.get("/{entity_id}/relations")
async def get_outgoing_relations(
    entity_id: UUID,
    tenant_id: str | None = None,
) -> list[dict]:
    entity = await _svc.entity_get_by_id(entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="Entity not found")
    t_id = tenant_id or entity.tenant_id
    rows = await _svc.relation_get_outgoing(entity_id, t_id)
    return [
        {
            "relation": orm_to_dict(rel, RELATION_FIELDS),
            "target": orm_to_dict(target, ENTITY_FIELDS),
        }
        for rel, target in rows
    ]
