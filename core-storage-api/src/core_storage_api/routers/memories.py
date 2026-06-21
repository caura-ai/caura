"""Memory CRUD, search, lifecycle, and dedup endpoints."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from common.events.lifecycle_purge_request import (
    MEMORY_RETENTION_MAX_DAYS,
    MEMORY_RETENTION_MIN_DAYS,
)
from core_storage_api.observability import bind_timer, log_request
from core_storage_api.schemas import MEMORY_FIELDS, MEMORY_LIST_FIELDS, orm_to_dict
from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/memories", tags=["Memories"])
_svc = PostgresService()


# ------------------------------------------------------------------
# Core CRUD (non-parameterised paths first)
# ------------------------------------------------------------------


_DATETIME_FIELDS = {
    "created_at",
    "expires_at",
    "deleted_at",
    "last_recalled_at",
    "ts_valid_start",
    "ts_valid_end",
    "last_dedup_checked_at",
}


def _parse_datetimes(body: dict) -> dict:
    """Convert ISO-format datetime strings to ``datetime`` objects.

    Malformed ISO strings raise ``HTTPException(422)`` rather than
    propagating the ``ValueError`` from ``datetime.fromisoformat`` as
    a 500 — a request whose body says ``"ts_valid_start": "tomorrow"``
    is a client validation problem, not a server fault. Applies to
    both POST and PATCH routes since both share this helper.
    """
    for key in _DATETIME_FIELDS:
        val = body.get(key)
        if isinstance(val, str):
            try:
                body[key] = datetime.fromisoformat(val)
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid ISO datetime for field {key!r}: {val!r}",
                )
    return body


@router.post("")
async def create_memory(request: Request) -> dict:
    body: dict = await request.json()
    _parse_datetimes(body)
    memory = await _svc.memory_add(body)
    return orm_to_dict(memory, MEMORY_FIELDS)


@router.post("/bulk")
async def create_memories_bulk(request: Request) -> list[dict]:
    """Insert a batch with per-attempt idempotency (CAURA-602).

    Each item must carry ``client_request_id`` (server-derived from
    ``X-Bulk-Attempt-Id`` upstream, or a UUID for in-process callers
    like auto-chunk). The response is per-item — ``{client_request_id,
    id, was_inserted}`` in input order — so the upstream core-api can
    map to ``created`` (was_inserted=True) vs ``duplicate_attempt``
    (False) without a second roundtrip. The full ORM dict was the prior
    contract; downstream callers reconstruct any other fields from the
    request payload they already hold.
    """
    body: list[dict] = await request.json()
    for item in body:
        _parse_datetimes(item)
    return await _svc.memory_add_all(body)


# ------------------------------------------------------------------
# Search
# ------------------------------------------------------------------


@router.post("/scored-search")
async def scored_search(request: Request) -> list[dict]:
    body: dict = await request.json()
    # Build search_params from top-level body keys (client sends them flat)
    _SEARCH_PARAM_KEYS = {
        "fts_weight",
        "freshness_floor",
        "freshness_decay_days",
        "recall_boost_cap",
        "recall_decay_window_days",
        "similarity_blend",
    }
    search_params = body.get("search_params") or {k: body[k] for k in _SEARCH_PARAM_KEYS if k in body}

    # Parse temporal_window from days (legacy) or seconds (pipeline path)
    temporal_window = None
    if body.get("temporal_window_days"):
        temporal_window = timedelta(days=body["temporal_window_days"])
    elif body.get("temporal_window_seconds"):
        temporal_window = timedelta(seconds=body["temporal_window_seconds"])

    # Hard date-range filter (pipeline path)
    date_range_start = body.get("date_range_start")
    date_range_end = body.get("date_range_end")

    # Parse valid_at ISO string to datetime
    valid_at = body.get("valid_at")
    if isinstance(valid_at, str):
        valid_at = datetime.fromisoformat(valid_at)

    t_start = time.perf_counter()
    db_timer = None
    out: list[dict] = []
    success = True
    try:
        with bind_timer() as db_timer:
            results = await _svc.memory_scored_search(
                tenant_id=body["tenant_id"],
                embedding=body["embedding"],
                query=body["query"],
                fleet_ids=body.get("fleet_ids"),
                caller_agent_id=body.get("caller_agent_id"),
                filter_agent_id=body.get("filter_agent_id"),
                memory_type_filter=body.get("memory_type_filter"),
                status_filter=body.get("status_filter"),
                valid_at=valid_at,
                boosted_memory_ids=set(body["boosted_memory_ids"])
                if body.get("boosted_memory_ids")
                else None,
                memory_boost_factor={UUID(k): v for k, v in body["memory_boost_factor"].items()}
                if body.get("memory_boost_factor")
                else None,
                search_params=search_params,
                temporal_window=temporal_window,
                recall_boost_enabled=body.get("recall_boost_enabled", True),
                top_k=body.get("top_k", 10),
                date_range_start=date_range_start,
                date_range_end=date_range_end,
                # Optional; absent for legacy single-tenant callers
                readable_tenant_ids=body.get("readable_tenant_ids") or None,
            )
        for r in results:
            row = orm_to_dict(r.Memory, MEMORY_FIELDS)
            row["score"] = float(r.score) if r.score is not None else 0.0
            row["similarity"] = float(r.similarity) if r.similarity is not None else 0.0
            row["vec_sim"] = float(r.vec_sim) if r.vec_sim is not None else 0.0
            # CAURA-594: authoritative signal for async-embed callers;
            # `vec_sim == 0.0` is ambiguous with an orthogonal embedding.
            # Default to False on missing attribute — the only readers
            # are workers deciding whether to re-embed, and a redundant
            # re-embed is harmless while a silent skip of a NULL row is
            # not.
            row["has_embedding"] = bool(getattr(r, "has_embedding", False))
            row["status_penalty"] = (
                float(r.status_penalty) if getattr(r, "status_penalty", None) is not None else 1.0
            )
            row["entity_links"] = r.entity_links or []
            out.append(row)
    except Exception:
        success = False
        raise
    finally:
        # body.get() here — never indexed access — so a malformed payload
        # that omits tenant_id doesn't raise a secondary KeyError in the
        # finally and swallow the original exception.
        log_request(
            "scored-search",
            tenant_id=body.get("tenant_id"),
            top_k=body.get("top_k", 10),
            total_ms=(time.perf_counter() - t_start) * 1000,
            db_ms=db_timer.total_ms if db_timer is not None else 0.0,
            row_count=len(out),
            has_date_range=bool(date_range_start and date_range_end),
            has_temporal_window=temporal_window is not None,
            error=not success,
        )
    return out


@router.post("/load-by-ids")
async def load_by_ids(request: Request) -> list[dict]:
    """Load memories by ID with visibility/fleet/agent filters applied.

    CAURA-687: dedicated endpoint for the ENTITY_LOOKUP short-circuit.
    Bypasses vector cosine + FTS + freshness scoring — the caller (
    ClassifyQuery._collect_memories in core-api) already picked the
    memory IDs via entity graph expansion and just needs them loaded
    with the standard visibility filters applied server-side.

    Previously this caller POSTed to ``/scored-search`` with a
    ``memory_ids`` key that route never read; the route hard-indexed
    ``body["embedding"]`` and 500'd, the broad except in classify_query
    swallowed it, and the short-circuit silently fell through to
    keyword/semantic search on every entity-token query.
    """
    body: dict = await request.json()

    t_start = time.perf_counter()
    db_timer = None
    out: list[dict] = []
    success = True
    # Parsed inside try so malformed-input failures (bad UUID, non-ISO
    # date) get captured by log_request in the finally rather than
    # bubbling up unlogged. Pre-declared so the finally can read them
    # safely even if the parse raised.
    memory_ids: list[UUID] = []
    try:
        memory_ids = [UUID(mid) for mid in body.get("memory_ids", [])]
        if not memory_ids:
            return []

        # Explicit 422 on missing tenant_id rather than letting body["tenant_id"]
        # raise a bare KeyError that the broad except below turns into an opaque
        # 500. Callers should know the difference between "I sent a bad payload"
        # and "the server blew up".
        tenant_id: str | None = body.get("tenant_id")
        if not tenant_id:
            raise HTTPException(status_code=422, detail="tenant_id is required")

        valid_at = body.get("valid_at")
        if isinstance(valid_at, str):
            valid_at = datetime.fromisoformat(valid_at)

        with bind_timer() as db_timer:
            memories = await _svc.memory_load_by_ids(
                memory_ids=memory_ids,
                tenant_id=tenant_id,
                fleet_ids=body.get("fleet_ids"),
                caller_agent_id=body.get("caller_agent_id"),
                filter_agent_id=body.get("filter_agent_id"),
                memory_type_filter=body.get("memory_type_filter"),
                status_filter=body.get("status_filter"),
                valid_at=valid_at,
                readable_tenant_ids=body.get("readable_tenant_ids") or None,
            )
        out = [orm_to_dict(m, MEMORY_FIELDS) for m in memories]
    except Exception:
        success = False
        raise
    finally:
        log_request(
            "load-by-ids",
            tenant_id=body.get("tenant_id"),
            total_ms=(time.perf_counter() - t_start) * 1000,
            db_ms=db_timer.total_ms if db_timer is not None else 0.0,
            row_count=len(out),
            id_count=len(memory_ids),
            error=not success,
        )
    return out


# ------------------------------------------------------------------
# Dedup / content hash
# ------------------------------------------------------------------


@router.post("/semantic-duplicate")
async def find_semantic_duplicate(request: Request) -> dict:
    body: dict = await request.json()
    result = await _svc.memory_find_semantic_duplicate(
        tenant_id=body["tenant_id"],
        fleet_id=body.get("fleet_id"),
        embedding=body["embedding"],
        exclude_id=UUID(body["exclude_id"]) if body.get("exclude_id") else None,
        visibility=body.get("visibility"),
        min_similarity=body.get("min_similarity"),
    )
    if result is None:
        raise HTTPException(status_code=404, detail="No semantic duplicate found")
    memory, similarity = result
    payload = orm_to_dict(memory, MEMORY_FIELDS)
    # A1 #16 — surface the score so the dispatching pipeline step can
    # pick auto-reject vs judge-dispatch vs accept by tier.
    payload["similarity"] = similarity
    return payload


@router.post("/entity-overlap-candidates")
async def find_entity_overlap_candidates(request: Request) -> list[dict]:
    body: dict = await request.json()
    memories = await _svc.memory_find_entity_overlap_candidates(
        memory_id=UUID(body["memory_id"]),
        tenant_id=body["tenant_id"],
        fleet_id=body.get("fleet_id"),
        visibility=body.get("visibility", "scope_team"),
        limit=body.get("limit", 8),
        include_supersedes=bool(body.get("include_supersedes", False)),
    )
    return [orm_to_dict(m, MEMORY_FIELDS) for m in memories]


@router.post("/find-successors")
async def find_successors(request: Request) -> list[dict]:
    body: dict = await request.json()
    valid_at = body.get("valid_at")
    if isinstance(valid_at, str):
        from datetime import datetime

        valid_at = datetime.fromisoformat(valid_at)
    memories = await _svc.memory_find_successors(
        supersedes_ids=[UUID(sid) for sid in body["supersedes_ids"]],
        tenant_id=body["tenant_id"],
        fleet_ids=body.get("fleet_ids"),
        caller_agent_id=body.get("caller_agent_id"),
        filter_agent_id=body.get("filter_agent_id"),
        memory_type_filter=body.get("memory_type_filter"),
        valid_at=valid_at,
    )
    return [orm_to_dict(m, MEMORY_FIELDS) for m in memories]


@router.post("/similar-candidates")
async def find_similar_candidates(request: Request) -> list[dict]:
    body: dict = await request.json()
    memories = await _svc.memory_find_similar_candidates(
        tenant_id=body["tenant_id"],
        fleet_id=body.get("fleet_id"),
        embedding=body["embedding"],
        memory_id=UUID(body["memory_id"]),
        visibility=body.get("visibility", "scope_team"),
        threshold=body.get("threshold", 0.7),
        limit=body.get("limit", 20),
    )
    return [orm_to_dict(m, MEMORY_FIELDS) for m in memories]


@router.get("/by-content-hash")
async def find_by_content_hash(
    tenant_id: str,
    content_hash: str,
    fleet_id: str | None = None,
    agent_id: str | None = None,
) -> dict:
    memory = await _svc.memory_find_by_content_hash(tenant_id, content_hash, fleet_id, agent_id=agent_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found by content hash")
    return orm_to_dict(memory, MEMORY_FIELDS)


@router.get("/embedding-by-content-hash")
async def find_embedding_by_content_hash(
    tenant_id: str,
    content_hash: str,
) -> list[float] | None:
    return await _svc.memory_find_embedding_by_content_hash(tenant_id, content_hash)


@router.get("/duplicate-hash")
async def find_duplicate_hash(
    tenant_id: str,
    content_hash: str,
    exclude_id: str | None = None,
) -> dict | None:
    dup_id = await _svc.memory_find_duplicate_hash(
        tenant_id,
        content_hash,
        exclude_id=UUID(exclude_id) if exclude_id else None,
    )
    if dup_id is None:
        return None
    return {"memory_id": str(dup_id)}


@router.post("/bulk-get")
async def bulk_get_memories(request: Request) -> list[dict | None]:
    """Fetch many memories by id in a single round-trip.

    Body: ``{"ids": ["uuid", ...], "tenant_id": "..." (optional)}``.

    Returns a list of memory dicts in the **same order** as the input ids,
    with ``null`` for ids that don't exist (or are soft-deleted, or — if
    ``tenant_id`` is provided — belong to a different tenant). Order
    preservation matters: callers (e.g. crystallizer archive sweep) zip
    the response back to their original id list.

    Tenant filter is optional to mirror the single-row ``GET /memories/{id}``
    contract. Callers that need tenant safety supply ``tenant_id``; the
    request fails open per-row (returns ``null``) when an id belongs to a
    different tenant, never with a 4xx.

    Cap: 1000 ids per request to bound query plan size and response payload.
    """
    body: dict = await request.json()
    raw_ids = body.get("ids", [])
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=422, detail="'ids' must be a list")
    if len(raw_ids) > 1000:
        raise HTTPException(
            status_code=422,
            detail=f"bulk-get capped at 1000 ids (got {len(raw_ids)})",
        )
    if not raw_ids:
        return []

    try:
        uids = [UUID(i) for i in raw_ids]
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=422, detail=f"invalid UUID in ids: {e}")

    tenant_filter = body.get("tenant_id")
    by_id = await _svc.memory_get_memories_by_ids(uids)

    out: list[dict | None] = []
    for uid in uids:
        mem = by_id.get(uid)
        if mem is None or (tenant_filter is not None and mem.tenant_id != tenant_filter):
            out.append(None)
        else:
            out.append(orm_to_dict(mem, MEMORY_FIELDS))
    return out


@router.post("/bulk-by-content-hashes")
async def bulk_find_by_content_hashes(request: Request) -> dict:
    """Wire format: ``{content_hash: {id, client_request_id}}``.

    See ``memory_bulk_find_by_content_hashes`` for why
    ``client_request_id`` is part of the response — the upstream bulk
    route uses it to distinguish ``duplicate_attempt`` from
    ``duplicate_content`` (CAURA-602). ``agent_id`` (optional) scopes
    the dedup lookup per Stage 5.
    """
    body: dict = await request.json()
    result = await _svc.memory_bulk_find_by_content_hashes(
        tenant_id=body["tenant_id"],
        hashes=body["hashes"],
        fleet_id=body.get("fleet_id"),
        agent_id=body.get("agent_id"),
    )
    return {ch: {"id": str(v["id"]), "client_request_id": v["client_request_id"]} for ch, v in result.items()}


@router.get("/rdf-conflicts")
async def find_rdf_conflicts(
    tenant_id: str,
    subject_entity_id: str,
    predicate: str,
    exclude_id: str | None = None,
    fleet_id: str | None = None,
    object_value: str | None = None,
) -> list[dict]:
    # CAURA-123 — forward ``fleet_id`` and ``object_value`` to the
    # service. Without ``object_value`` the previous default of ``""``
    # made the SQL filter ``object_value != ''`` return every non-empty
    # value — including the new memory's own value — producing false
    # RDF conflicts for two writes of the same fact (e.g., punctuation
    # differences). With it forwarded the service's ``!=`` filter
    # correctly excludes same-value rows.
    memories = await _svc.memory_find_rdf_conflicts(
        tenant_id=tenant_id,
        subject_entity_id=UUID(subject_entity_id),
        predicate=predicate,
        object_value=object_value or "",
        memory_id=UUID(exclude_id) if exclude_id else UUID(int=0),
        fleet_id=fleet_id,
    )
    return [orm_to_dict(m, MEMORY_FIELDS) for m in memories]


@router.post("/near-duplicates")
async def check_near_duplicates(request: Request) -> dict:
    body: dict = await request.json()
    candidates = await _svc.memory_find_near_duplicate_candidates(
        tenant_id=body["tenant_id"],
        fleet_id=body.get("fleet_id"),
        batch_size=body.get("batch_size", 100),
        offset=body.get("offset", 0),
    )
    return {"candidates": [{"id": str(r[0]), "embedding": r[1]} for r in candidates]}


@router.post("/neighbors-by-embedding")
async def find_neighbors_by_embedding(request: Request) -> list[dict]:
    body: dict = await request.json()
    rows = await _svc.memory_find_neighbors_by_embedding(
        tenant_id=body["tenant_id"],
        fleet_id=body.get("fleet_id"),
        query_embedding=body["query_embedding"],
        exclude_id=UUID(body["exclude_id"]),
        threshold=body.get("threshold", 0.95),
        limit=body.get("limit", 5),
    )
    return [{"id": str(r[0]), "similarity": float(r[1])} for r in rows]


@router.post("/mark-dedup-checked")
async def mark_dedup_checked(request: Request) -> dict:
    body: dict = await request.json()
    memory_ids = [UUID(mid) for mid in body["memory_ids"]]
    await _svc.memory_mark_dedup_checked(memory_ids)
    return {"ok": True}


@router.post("/entity-links")
async def get_entity_links_for_memories(request: Request) -> dict:
    body: dict = await request.json()
    memory_ids = [UUID(mid) for mid in body["memory_ids"]]
    links = await _svc.memory_get_entity_links_for_memories(memory_ids)
    # Serialise UUID keys to strings
    return {
        str(k): [{"entity_id": str(el["entity_id"]), "role": el["role"]} for el in v]
        for k, v in links.items()
    }


# ------------------------------------------------------------------
# Batch status
# ------------------------------------------------------------------


@router.post("/batch-update-status")
async def batch_update_status(request: Request) -> dict:
    """Apply status (and optional supersedes-id) updates to many memories.

    Per-row payload:
      - ``memory_id`` (required), ``status`` (required)
      - ``supersedes_id`` (optional, UUID): set the pointer to this id
      - ``unset_supersedes`` (optional, bool): clear the pointer; takes
        precedence over ``supersedes_id`` if both are present
      - ``expected_supersedes_id`` (optional, UUID): CAS gate — skip the
        row unless its current ``supersedes_id`` matches this value

    Backward-compatible with the prior 2-field shape — rows containing
    only ``memory_id`` + ``status`` behave exactly as before.

    Returns ``{"ok": True, "skipped": [memory_id, ...]}`` listing rows
    that failed the CAS gate (or pointed at a deleted / nonexistent id).
    Callers that don't use the CAS field can safely ignore ``skipped``.
    """
    body: dict = await request.json()

    # The batch is per-tenant: every memory in one batch shares the trigger
    # tenant, so ``tenant_id`` is carried at the batch level (not per row) and
    # threaded into each ``memory_update_status`` as the cross-tenant write
    # guard. Explicit 422 on missing tenant_id, mirroring the sibling routes.
    tenant_id: str | None = body.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise HTTPException(
            status_code=422,
            detail="'tenant_id' is required and must be a non-empty string",
        )

    # Two passes: validate everything FIRST, then execute. A malformed
    # item in the middle of the batch would otherwise commit rows
    # 0..K-1 before the 422 lands, leaving the caller no receipt of
    # what got written. The validation pass is O(N) memory + zero DB
    # work, so the cost is negligible compared to the partial-commit
    # surprise it prevents.
    parsed: list[tuple[UUID, str, UUID | None, bool, UUID | None]] = []
    for item in body.get("updates", []):
        try:
            sup_id = item.get("supersedes_id")
            exp_sup_id = item.get("expected_supersedes_id")
            parsed.append(
                (
                    UUID(item["memory_id"]),
                    item["status"],
                    UUID(sup_id) if sup_id else None,
                    bool(item.get("unset_supersedes", False)),
                    UUID(exp_sup_id) if exp_sup_id else None,
                )
            )
        except (ValueError, KeyError) as exc:
            # ``ValueError`` from ``UUID(...)`` includes the raw input
            # in its message ("badly formed hexadecimal UUID string: ...")
            # — surfacing ``exc`` verbatim would echo the offending
            # value back across the API boundary. Use a generic
            # field-hint instead.
            field_hint = "memory_id or status" if isinstance(exc, KeyError) else "a UUID field"
            raise HTTPException(
                status_code=422,
                detail=(
                    f"invalid update item "
                    f"(memory_id={item.get('memory_id', '?')!r}): "
                    f"{type(exc).__name__} in {field_hint}"
                ),
            )

    skipped: list[str] = []
    for mid, new_status, sup_uuid, unset_sup, exp_sup_uuid in parsed:
        ok = await _svc.memory_update_status(
            mid,
            new_status,
            tenant_id=tenant_id,
            supersedes_id=sup_uuid,
            unset_supersedes=unset_sup,
            expected_supersedes_id=exp_sup_uuid,
        )
        if not ok:
            skipped.append(str(mid))
    return {"ok": True, "skipped": skipped}


# ------------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------------


@router.post("/archive-expired")
async def archive_expired(request: Request) -> dict:
    body: dict = await request.json()
    count = await _svc.memory_archive_expired(
        tenant_id=body["tenant_id"],
        fleet_id=body.get("fleet_id"),
        batch_size=body.get("batch_size", 500),
    )
    return {"count": count}


@router.post("/archive-stale")
async def archive_stale_low_weight(request: Request) -> dict:
    body: dict = await request.json()
    count = await _svc.memory_archive_stale(
        tenant_id=body["tenant_id"],
        fleet_id=body.get("fleet_id"),
        stale_days=body.get("stale_days", 90),
        max_weight=body.get("max_weight", 0.3),
        batch_size=body.get("batch_size", 500),
    )
    return {"count": count}


@router.post("/purge-soft-deleted")
async def purge_soft_deleted(request: Request) -> dict:
    """Hard-delete soft-deleted memories older than ``retention_days``
    (CAURA-656). The retention window is policy, not state — the caller
    decides how long ``deleted_at IS NOT NULL`` rows stick around for
    undo / forensics. ``retention_days`` defaults to 30 to match the
    organization-settings default.
    """
    body: dict = await request.json()
    # Validate the inputs that drive the SQL primitive's WHERE clause.
    # ``tenant_id`` missing would 500 on a KeyError; ``retention_days=0``
    # produces ``deleted_at < NOW()`` (matches every soft-deleted row,
    # nullifying the retention window); ``batch_size`` 0 silently
    # no-ops every tick (Postgres LIMIT 0) and -1 unbounds the
    # delete. Each failure mode would be a real outage from the
    # consumer's perspective; surface as 422 at the boundary.
    tenant_id = body.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise HTTPException(
            status_code=422,
            detail="'tenant_id' is required and must be a non-empty string",
        )
    batch_size = body.get("batch_size", 500)
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise HTTPException(
            status_code=422,
            detail="'batch_size' must be a positive integer",
        )
    retention_days = body.get("retention_days", MEMORY_RETENTION_MAX_DAYS)
    if (
        not isinstance(retention_days, int)
        or isinstance(retention_days, bool)
        or not (MEMORY_RETENTION_MIN_DAYS <= retention_days <= MEMORY_RETENTION_MAX_DAYS)
    ):
        raise HTTPException(
            status_code=422,
            detail=f"'retention_days' must be in [{MEMORY_RETENTION_MIN_DAYS}, {MEMORY_RETENTION_MAX_DAYS}]",
        )
    count = await _svc.memory_purge_soft_deleted(
        tenant_id=tenant_id,
        fleet_id=body.get("fleet_id"),
        retention_days=retention_days,
        batch_size=batch_size,
    )
    return {"deleted": count}


# ------------------------------------------------------------------
# Stats / analytics
# ------------------------------------------------------------------


@router.get("/stats")
async def get_memory_stats(
    tenant_id: str,
    fleet_id: str | None = None,
) -> dict:
    return await _svc.memory_compute_health_stats(tenant_id, fleet_id)


@router.get("/embedding-coverage")
async def get_embedding_coverage(
    tenant_id: str,
    fleet_id: str | None = None,
) -> dict:
    missing = await _svc.memory_find_missing_embeddings(tenant_id, fleet_id)
    total = await _svc.memory_count_active(tenant_id, fleet_id)
    return {
        "total_active": total,
        "missing_embeddings": len(missing),
        "coverage_pct": round((total - len(missing)) / total * 100, 1) if total > 0 else 0.0,
    }


@router.get("/type-distribution")
async def get_type_distribution(
    tenant_id: str,
    fleet_id: str | None = None,
) -> dict:
    stats = await _svc.memory_compute_health_stats(tenant_id, fleet_id)
    return {"type_distribution": stats.get("type_distribution", {})}


@router.get("/recent")
async def get_recent_memories(
    tenant_id: str,
    fleet_id: str | None = None,
    limit: int = 20,
) -> list[dict]:
    memories = await _svc.memory_list_recent(tenant_id, fleet_id, limit=limit)
    return [orm_to_dict(m, MEMORY_FIELDS) for m in memories]


@router.get("/lifecycle-candidates")
async def get_lifecycle_candidates(tenant_id: str) -> dict:
    expired = await _svc.memory_find_expired_still_active(tenant_id, None)
    stale = await _svc.memory_find_stale_count(tenant_id, None, stale_days=90, max_weight=0.3)
    return {
        "expired_still_active": [str(r[0]) for r in expired],
        "stale_low_weight": [str(r[0]) for r in stale],
    }


@router.get("/count")
async def count_memories(
    tenant_id: str,
    fleet_id: str | None = None,
) -> dict:
    if not tenant_id:
        count = await _svc.memory_count_all()
    else:
        count = await _svc.memory_count_active(tenant_id, fleet_id)
    return {"count": count}


@router.get("/count-active")
async def count_active_memories(tenant_id: str, fleet_id: str | None = None) -> dict:
    count = await _svc.memory_count_active(tenant_id, fleet_id)
    return {"count": count}


@router.get("/null-embedding-ids")
async def list_null_embedding_ids(
    tenant_id: str,
    limit: int = 500,
    after: str | None = None,
) -> dict:
    """Page through memories where ``embedding IS NULL`` for one tenant.

    Drives the event-driven embedding backfill task in core-worker
    (``core_worker.backfill``) — the worker fetches a page of ids
    here, calls ``GET /memories/{id}?tenant_id=...`` per row to
    retrieve the content, then publishes one ``EMBED_REQUESTED`` event
    per row. See ``local_emb_res/specs/C-backfill-task-pr.md``.

    Each returned row carries only the **identifiers** the worker
    needs to address the next fetch — ``id`` and ``tenant_id``. The
    raw ``content`` and ``content_hash`` are deliberately NOT inlined
    here. Two reasons:

    1. **Defence-in-depth.** The OSS storage API has no auth
       middleware (see ``app.py``); a successful GET on this endpoint
       must not leak every memory's content in a single response. An
       attacker who guesses the per-tenant id still has to issue
       one ``GET /memories/{id}`` per row, which is rate-limitable
       and audit-logged separately.
    2. **Payload size.** A 5000-row page with full content can run to
       multiple MB; ids-only keeps it deterministic and small
       regardless of corpus shape.

    Idempotent under restart: the consumer's writes flip rows from
    NULL to non-NULL, so a re-run picks up only rows that are still
    NULL.

    ``tenant_id`` is required for the same defence-in-depth reason —
    no un-scoped scans across all tenants. For whole-deployment
    scans, iterate the tenant list externally and call this endpoint
    once per tenant.
    """
    if limit < 1 or limit > 5000:
        raise HTTPException(
            status_code=422,
            detail="limit must be in [1, 5000]",
        )
    after_uuid: UUID | None = None
    if after is not None:
        try:
            after_uuid = UUID(after)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"after must be a UUID, got {after!r}",
            )

    rows, total_remaining = await _svc.memory_list_null_embedding_rows(
        limit=limit, after=after_uuid, tenant_id=tenant_id
    )
    return {
        "rows": [{"id": str(row_id), "tenant_id": row_tenant} for row_id, row_tenant in rows],
        "next_after": str(rows[-1][0]) if rows else None,
        "total_remaining": total_remaining,
    }


@router.get("/distinct-agents")
async def count_distinct_agents() -> dict:
    """Global count of distinct agent identities across all memories.

    Used by the public landing-page Agents counter.
    """
    count = await _svc.memory_distinct_agent_count()
    return {"count": count}


@router.get("/distinct-tenants")
async def count_distinct_tenants() -> dict:
    """Global count of distinct tenants with at least one live memory.

    Used by the public landing-page Tenants counter — replaces the
    hardcoded ``1`` previously returned by ``/api/v1/stats``.
    """
    count = await _svc.memory_distinct_tenant_count()
    return {"count": count}


# ------------------------------------------------------------------
# A1 #18 — Dedup review queue
# ------------------------------------------------------------------


_DEDUP_REVIEW_FIELDS = [
    "id",
    "tenant_id",
    "fleet_id",
    "agent_id",
    "new_memory_id",
    "candidate_memory_id",
    "new_content",
    "candidate_content",
    "similarity",
    "judge_verdict",
    "judge_confidence",
    "decision_band",
    "status",
    "decided_at",
    "decided_by",
    "created_at",
]


@router.post("/dedup-reviews")
async def enqueue_dedup_review(request: Request) -> dict:
    body: dict = await request.json()
    try:
        review = await _svc.dedup_review_enqueue(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return orm_to_dict(review, _DEDUP_REVIEW_FIELDS)


@router.get("/dedup-reviews")
async def list_dedup_reviews(
    tenant_id: str,
    status: str = "pending",
    limit: int = 50,
) -> list[dict]:
    reviews = await _svc.dedup_review_list(tenant_id, status=status, limit=limit)
    return [orm_to_dict(r, _DEDUP_REVIEW_FIELDS) for r in reviews]


@router.post("/dedup-reviews/{review_id}/decision")
async def decide_dedup_review(review_id: UUID, request: Request) -> dict:
    body: dict = await request.json()
    status = body.get("status")
    decided_by = body.get("decided_by")
    if not isinstance(status, str):
        raise HTTPException(status_code=400, detail="status (string) required")
    try:
        review = await _svc.dedup_review_decide(review_id, status, decided_by=decided_by)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")
    return orm_to_dict(review, _DEDUP_REVIEW_FIELDS)


# ------------------------------------------------------------------
# Fix 2 Phase 2 — fleet/admin discovery + detail + bulk mutations
#
# Literal-path routes; MUST stay above the parameterised ``/{memory_id}``
# block below so segments like ``fleet-distribution`` / ``admin-list`` /
# ``redistribute`` aren't parsed as a memory UUID.
# ------------------------------------------------------------------


@router.get("/fleet-distribution")
async def fleet_distribution(
    tenant_id: str | None = None,
    exclude_scope_agent: bool = False,
) -> list[dict]:
    """Distinct ``fleet_id`` with memory + agent counts, desc.

    Serves both ``GET /fleets`` (``exclude_scope_agent=true``) and
    ``GET /admin/fleets`` (``exclude_scope_agent=false``, cross-tenant when
    ``tenant_id`` is omitted) upstream.
    """
    return await _svc.memory_fleet_distribution(tenant_id, exclude_scope_agent=exclude_scope_agent)


@router.get("/admin-stats")
async def admin_stats(
    tenant_id: str | None = None,
    fleet_id: str | None = None,
) -> dict:
    """Admin memory stats — ``{total, by_type, by_agent, by_status}``.

    Single GROUPING SETS scan, no visibility scoping, cross-tenant when
    ``tenant_id`` is omitted.
    """
    return await _svc.memory_admin_stats(tenant_id, fleet_id)


@router.post("/admin-list")
async def admin_list(request: Request) -> list[dict]:
    """Admin cross-tenant memory list (NO visibility scoping).

    Body: ``{tenant_id?, fleet_id?, agent_id?, memory_type?, status?,
    include_deleted, sort, order, offset, limit, cursor_ts?, cursor_id?}``.
    Returns up to ``limit`` rows in input cursor order — the caller passes
    ``limit`` already widened to ``limit+1`` and slices / builds the next
    cursor itself.
    """
    body: dict = await request.json()
    # Guard cursor parsing (matches the sibling soft-delete/redistribute routes):
    # a malformed cursor in the raw body must 422, not 500 on an unhandled
    # ValueError/TypeError from fromisoformat / UUID.
    cursor_ts_raw = body.get("cursor_ts")
    cursor_id_raw = body.get("cursor_id")
    try:
        cursor_ts = datetime.fromisoformat(cursor_ts_raw) if isinstance(cursor_ts_raw, str) else cursor_ts_raw
        cursor_id = UUID(cursor_id_raw) if cursor_id_raw else None
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid cursor fields: {exc}") from exc
    memories = await _svc.memory_admin_list(
        tenant_id=body.get("tenant_id"),
        fleet_id=body.get("fleet_id"),
        agent_id=body.get("agent_id"),
        memory_type=body.get("memory_type"),
        status=body.get("status"),
        include_deleted=bool(body.get("include_deleted", False)),
        sort=body.get("sort", "created_at"),
        order=body.get("order", "desc"),
        offset=body.get("offset", 0),
        limit=body.get("limit", 50),
        cursor_ts=cursor_ts,
        cursor_id=cursor_id,
    )
    # MEMORY_LIST_FIELDS (no embedding/search_vector): core-api's _memory_to_out
    # discards the vector, so don't ship it over the wire for a list.
    return [orm_to_dict(m, MEMORY_LIST_FIELDS) for m in memories]


@router.post("/list")
async def list_by_filters(request: Request) -> list[dict]:
    """Non-admin memory list WITH visibility scoping (MCP ``memclaw_list``).

    Body: ``{tenant_id, caller_agent_id?, fleet_id?, written_by?, memory_type?,
    status?, run_id?, weight_min?, weight_max?, created_after?, created_before?,
    include_deleted, sort, order, limit, offset, cursor_ts?, cursor_id?,
    readable_tenant_ids?}``. ``limit`` is the caller's desired page size; this
    endpoint over-fetches ``limit+1`` rows internally for has_more detection and
    the caller slices to ``limit`` / builds the next cursor. Distinct from
    ``/admin-list`` which has NO visibility scoping.
    """
    body: dict = await request.json()
    tenant_id = body.get("tenant_id")
    if not tenant_id:
        raise HTTPException(status_code=422, detail="tenant_id is required")
    # Cap the page size: the service over-fetches ``limit + 1``, so an
    # unbounded ``limit`` would let a caller pull the whole table in one
    # request. [1, 5000] matches the /null-embedding-ids bound in this file.
    raw_limit = body.get("limit", 25)
    try:
        limit = max(1, min(int(raw_limit), 5000))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="'limit' must be a positive integer") from None
    cursor_ts_raw = body.get("cursor_ts")
    cursor_id_raw = body.get("cursor_id")
    created_after_raw = body.get("created_after")
    created_before_raw = body.get("created_before")
    try:
        cursor_ts = datetime.fromisoformat(cursor_ts_raw) if isinstance(cursor_ts_raw, str) else cursor_ts_raw
        cursor_id = UUID(cursor_id_raw) if cursor_id_raw else None
        created_after = (
            datetime.fromisoformat(created_after_raw)
            if isinstance(created_after_raw, str)
            else created_after_raw
        )
        created_before = (
            datetime.fromisoformat(created_before_raw)
            if isinstance(created_before_raw, str)
            else created_before_raw
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid datetime fields: {exc}") from exc
    memories = await _svc.memory_list_by_filters(
        tenant_id=tenant_id,
        caller_agent_id=body.get("caller_agent_id"),
        fleet_id=body.get("fleet_id"),
        written_by=body.get("written_by"),
        memory_type=body.get("memory_type"),
        status=body.get("status"),
        run_id=body.get("run_id"),
        weight_min=body.get("weight_min"),
        weight_max=body.get("weight_max"),
        created_after=created_after,
        created_before=created_before,
        include_deleted=bool(body.get("include_deleted", False)),
        sort=body.get("sort", "created_at"),
        order=body.get("order", "desc"),
        limit=limit,
        offset=body.get("offset", 0),
        cursor_ts=cursor_ts,
        cursor_id=cursor_id,
        readable_tenant_ids=body.get("readable_tenant_ids"),
    )
    return [orm_to_dict(m, MEMORY_LIST_FIELDS) for m in memories]


@router.post("/stats-breakdown")
async def stats_breakdown(request: Request) -> dict:
    """Visibility-scoped stats breakdown (MCP ``memclaw_stats``).

    Body: ``{tenant_id?, fleet_id?, agent_id?, memory_type?, status?,
    include_deleted?, readable_tenant_ids?}``. Returns ``{total, by_type,
    by_agent, by_status}`` plus optional ``by_tenant`` (when the readable set
    spans >1 tenant) and ``deleted`` / ``total_including_deleted`` (when
    ``include_deleted``). Distinct from ``/admin-stats`` (no scoping) and
    ``/stats`` (health-stats shape).
    """
    body: dict = await request.json()
    tenant_id = body.get("tenant_id")
    if not tenant_id:
        # Mirror /list: a binding/home tenant is mandatory. Without it (and with no
        # readable set) the aggregation would run unscoped across all tenants.
        raise HTTPException(status_code=422, detail="tenant_id is required")
    return await _svc.memory_stats_breakdown(
        tenant_id=tenant_id,
        fleet_id=body.get("fleet_id"),
        agent_id=body.get("agent_id"),
        memory_type=body.get("memory_type"),
        status=body.get("status"),
        include_deleted=bool(body.get("include_deleted", False)),
        readable_tenant_ids=body.get("readable_tenant_ids"),
    )


@router.post("/soft-delete-by-filter")
async def soft_delete_by_filter(request: Request) -> dict:
    """Soft-delete every matching live memory for a tenant.

    Body: ``{tenant_id, fleet_id?, agent_id?, memory_type?, status?,
    exclude_ids?[], metadata_filter?{k:v}}``. The ≤20-pair + string-value
    validation stays in core-api (for its exact 400 messages); this route
    builds the JSONB predicate via SQLAlchemy bound params.
    """
    body: dict = await request.json()
    tenant_id = body.get("tenant_id")
    if not tenant_id:
        raise HTTPException(status_code=422, detail="tenant_id is required")
    try:
        exclude_ids = [UUID(i) for i in body.get("exclude_ids") or []]
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid UUID in exclude_ids: {exc}")
    metadata_filter = body.get("metadata_filter") or None
    if metadata_filter is not None and not isinstance(metadata_filter, dict):
        raise HTTPException(status_code=422, detail="metadata_filter must be an object")
    deleted = await _svc.memory_soft_delete_by_filter(
        tenant_id=tenant_id,
        fleet_id=body.get("fleet_id"),
        agent_id=body.get("agent_id"),
        memory_type=body.get("memory_type"),
        status=body.get("status"),
        exclude_ids=exclude_ids,
        metadata_filter=metadata_filter,
    )
    return {"deleted": deleted}


@router.post("/soft-delete-by-ids")
async def soft_delete_by_ids(request: Request) -> dict:
    """Soft-delete live memories by id (tenant-scoped). Body: ``{tenant_id,
    ids[]}``. The 1-1000 cap stays in core-api."""
    body: dict = await request.json()
    tenant_id = body.get("tenant_id")
    if not tenant_id:
        raise HTTPException(status_code=422, detail="tenant_id is required")
    try:
        ids = [UUID(i) for i in body.get("ids") or []]
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid UUID in ids: {exc}")
    deleted = await _svc.memory_soft_delete_by_ids(tenant_id, ids)
    return {"deleted": deleted}


@router.post("/soft-delete-by-run")
async def soft_delete_by_run(request: Request) -> dict:
    """Soft-delete live memories tagged with ``run_id`` AND
    ``metadata.source = metadata_source``. Body: ``{tenant_id, run_id,
    metadata_source}``."""
    body: dict = await request.json()
    tenant_id = body.get("tenant_id")
    run_id = body.get("run_id")
    if not tenant_id or not run_id:
        raise HTTPException(status_code=422, detail="tenant_id and run_id are required")
    deleted = await _svc.memory_soft_delete_by_run(
        tenant_id,
        run_id,
        metadata_source=body.get("metadata_source", "ingest"),
    )
    return {"deleted": deleted}


@router.post("/redistribute")
async def redistribute(request: Request) -> dict:
    """Bulk-reassign memories to ``target_agent_id`` in ONE transaction.

    Body: ``{tenant_id, memory_ids[], target_agent_id}``. Returns
    ``{moved, promoted, skipped, from_agents[], not_found[]}``. Trust gates
    + the agent_id==auth precedence check stay in core-api.
    """
    body: dict = await request.json()
    tenant_id = body.get("tenant_id")
    target_agent_id = body.get("target_agent_id")
    if not tenant_id or not target_agent_id:
        raise HTTPException(status_code=422, detail="tenant_id and target_agent_id are required")
    try:
        memory_ids = [UUID(i) for i in body.get("memory_ids") or []]
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid UUID in memory_ids: {exc}")
    return await _svc.memory_redistribute(
        tenant_id=tenant_id,
        memory_ids=memory_ids,
        target_agent_id=target_agent_id,
    )


# ------------------------------------------------------------------
# Parameterised paths — MUST come last to avoid catching /count etc.
# ------------------------------------------------------------------


@router.get("/{memory_id}/detail")
async def get_memory_detail(memory_id: UUID, tenant_id: str) -> dict:
    """Full memory row + entity links + server-computed embedding stats.

    The raw pgvector is never returned — only a first-20 preview and
    {dimensions,min,max,mean,non_zero}. 404 when the row is absent,
    soft-deleted, or belongs to another tenant.
    """
    detail = await _svc.memory_get_detail(memory_id, tenant_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return detail


@router.get("/{memory_id}/contradictions")
async def get_memory_contradictions(memory_id: UUID, tenant_id: str) -> dict:
    """Raw contradiction rows: ``{memory, supersessors[], older|null}``.

    The cross-tenant ``older`` guard is enforced server-side; core-api keeps
    the reason/direction/detection_status shaping. 404 when the target
    memory is absent/soft-deleted/wrong tenant.
    """
    rows = await _svc.memory_contradiction_rows(memory_id, tenant_id)
    if rows is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return rows


@router.get("/{memory_id}")
async def get_memory(memory_id: UUID, tenant_id: str | None = None) -> dict:
    t_start = time.perf_counter()
    db_timer = None
    memory = None
    success = True
    try:
        with bind_timer() as db_timer:
            if tenant_id is not None:
                memory = await _svc.memory_get_by_id_for_tenant(memory_id, tenant_id)
            else:
                memory = await _svc.memory_get_by_id(memory_id)
    except Exception:
        success = False
        raise
    finally:
        log_request(
            "memory-get",
            tenant_id=tenant_id,
            total_ms=(time.perf_counter() - t_start) * 1000,
            db_ms=db_timer.total_ms if db_timer is not None else 0.0,
            hit=memory is not None,
            error=not success,
        )
    if memory is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return orm_to_dict(memory, MEMORY_FIELDS)


@router.patch("/{memory_id}")
async def update_memory(memory_id: UUID, request: Request) -> dict:
    body: dict = await request.json()
    # ``_parse_datetimes`` mirrors the POST route's ingress contract:
    # asyncpg requires datetime instances on ``DateTime(timezone=True)``
    # columns and rejects ISO strings with ``CannotCoerceError``. The
    # CAURA-595 async-enrich worker hits this path with bare ISO
    # strings via ``model_dump(mode="json")``; the POST route always
    # parsed but the PATCH route silently passed strings straight to
    # SQLAlchemy → asyncpg → 500. Parse here so all callers (worker,
    # core-api, future tooling) get the same coercion at the API
    # boundary.
    _parse_datetimes(body)
    # No empty-body short-circuit here: ``memory_update`` runs the
    # existence check first and returns False for absent/soft-deleted
    # rows regardless of whether the body has actionable columns. An
    # earlier short-circuit would let a PATCH ``{}`` on a deleted row
    # answer 200 — inconsistent with the 404 the same row gets on a
    # non-empty PATCH.
    found = await _svc.memory_update(memory_id, body or {})
    if not found:
        # Pre-this-fix the route returned ``200 {"ok": True}`` regardless
        # of whether the row was missing or soft-deleted, so a PATCH on
        # a deleted memory looked successful to the caller while
        # silently no-op'ing. Surface as 404 so clients can distinguish
        # "applied" from "ignored because the row is gone."
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
    return {"ok": True}


@router.patch("/{memory_id}/status")
async def update_memory_status(memory_id: UUID, request: Request) -> dict:
    body: dict = await request.json()
    status = body["status"]
    supersedes_id = body.get("supersedes_id")
    unset_supersedes = bool(body.get("unset_supersedes", False))
    expected_supersedes_id = body.get("expected_supersedes_id")

    # ``tenant_id`` scopes every write path in this route (the CAS retraction,
    # the ``memory_update_status`` status flip, and the set-supersedes update)
    # so a caller in tenant B can't touch tenant A's memory by id. Explicit 422
    # on a missing tenant_id, mirroring the sibling routes.
    tenant_id: str | None = body.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        raise HTTPException(
            status_code=422,
            detail="'tenant_id' is required and must be a non-empty string",
        )

    if unset_supersedes and supersedes_id is not None:
        raise HTTPException(
            status_code=422,
            detail="supersedes_id and unset_supersedes are mutually exclusive",
        )
    if unset_supersedes and not expected_supersedes_id:
        raise HTTPException(
            status_code=422,
            detail="unset_supersedes=True requires expected_supersedes_id",
        )

    if unset_supersedes:
        # A4 #10 — retraction: clear ``supersedes_id`` AND set status in a
        # single SQL statement, guarded by CAS that requires the row's
        # current pointer to match ``expected_supersedes_id`` OR be
        # already NULL (idempotent re-fire). A pointer to *a different*
        # non-NULL uuid means another writer took the row in the meantime;
        # reject with 409 so the caller knows their view was stale.
        # Status + pointer update must be atomic — a partial update
        # (status advances but pointer clear is rejected) would leave
        # the row in an invalid state. Caught wet-testing 2026-05-19.
        from sqlalchemy import or_
        from sqlalchemy import update as sql_update

        from common.models import Memory
        from core_storage_api.services.postgres_service import get_session

        expected_uuid = UUID(expected_supersedes_id)
        async with get_session() as session:
            result = await session.execute(
                sql_update(Memory)
                .where(
                    Memory.id == memory_id,
                    Memory.tenant_id == tenant_id,
                    or_(
                        Memory.supersedes_id == expected_uuid,
                        Memory.supersedes_id.is_(None),
                    ),
                )
                .values(supersedes_id=None, status=status)
            )
            if result.rowcount == 0:  # type: ignore[attr-defined]
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "stale_retraction",
                        "memory_id": str(memory_id),
                        "expected_supersedes_id": expected_supersedes_id,
                    },
                )
        return {"ok": True}

    # Set or status-only paths. ``memory_update_status`` returns False
    # when the target row doesn't exist (or was already deleted); surface
    # as 404 so the caller doesn't silently treat a no-op as success.
    ok = await _svc.memory_update_status(memory_id, status, tenant_id=tenant_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"memory {memory_id} not found")

    if supersedes_id is not None:
        # Set path: compare-and-swap against NULL — the first detection
        # to land owns the chain. Later re-fires on the same row no-op
        # at the DB. Pairs with the created_at direction invariant in
        # core_api.services.contradiction_detector to defend the
        # CAURA-000 ``NEW.supersedes_id = OLD.id`` rule against re-fired
        # detection on already-resolved memories.
        from sqlalchemy import update as sql_update

        from common.models import Memory
        from core_storage_api.services.postgres_service import get_session

        async with get_session() as session:
            await session.execute(
                sql_update(Memory)
                .where(
                    Memory.id == memory_id,
                    Memory.tenant_id == tenant_id,
                    Memory.supersedes_id.is_(None),
                )
                .values(supersedes_id=UUID(supersedes_id))
            )
    return {"ok": True}


@router.patch("/{memory_id}/embedding")
async def update_embedding(memory_id: UUID, request: Request) -> dict:
    body: dict = await request.json()
    await _svc.memory_update_embedding(
        memory_id,
        embedding=body["embedding"],
        metadata=body.get("metadata"),
    )
    return {"ok": True}


@router.patch("/{memory_id}/entities")
async def update_memory_entities(memory_id: UUID, request: Request) -> dict:
    body: dict = await request.json()
    entity_links = body.get("entity_links", [])
    if not isinstance(entity_links, list):
        raise HTTPException(status_code=422, detail="'entity_links' must be a list")
    try:
        links = [{"entity_id": UUID(link["entity_id"]), "role": link["role"]} for link in entity_links]
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if any(not isinstance(lnk["role"], str) or not lnk["role"] for lnk in links):
        raise HTTPException(status_code=422, detail="Each entity link must have a non-empty string 'role'")
    await _svc.memory_add_entity_links(memory_id, links)
    return {"ok": True}


@router.delete("/{memory_id}")
async def soft_delete_memory(memory_id: UUID) -> dict:
    await _svc.memory_soft_delete(memory_id)
    return {"ok": True}
