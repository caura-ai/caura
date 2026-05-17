import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from uuid import UUID

import httpx
from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, tuple_, update
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.exc import TimeoutError as SQLATimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.memory import Memory
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.config import settings as app_settings
from core_api.constants import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    MEMORY_VISIBILITY_SCOPE_AGENT,
)
from core_api.db.session import get_db
from core_api.middleware.idempotency import (
    IDEMPOTENCY_HEADER,
    IdempotencyGuard,
    idempotency_for,
    idempotency_key_from_metadata,
)
from core_api.middleware.per_tenant_concurrency import per_tenant_slot
from core_api.middleware.rate_limit import search_limit, write_bulk_limit, write_limit
from core_api.pagination import decode_cursor, encode_cursor, paginated_order_by
from core_api.schemas import (
    BulkMemoryCreate,
    BulkMemoryResponse,
    IngestCommitRequest,
    IngestRequest,
    MemoryCreate,
    MemoryOut,
    MemoryUpdate,
    PaginatedMemoryResponse,
    RedistributeRequest,
    RedistributeResponse,
    SearchRequest,
    SearchResponse,
    STMWriteResponse,
    UsageSummary,
)
from core_api.services.agent_service import (
    enforce_delete,
    enforce_fleet_read,
    enforce_fleet_write,
    get_or_create_agent,
    lookup_agent,
)
from core_api.services.audit_service import log_action
from core_api.services.ingest_service import (
    ALLOWED_INGEST_MIME_TYPES,
    BINARY_INGEST_MIME_TYPES,
    INGEST_DOCUMENTS_COLLECTION,
    INGEST_MAX_INPUT_BYTES,
    TEXT_INGEST_MIME_TYPES,
    _extract_with_kreuzberg,
    decode_text_body,
    ingest_commit,
    ingest_preview,
)
from core_api.services.memory_service import (
    _memory_to_out,
    create_memories_bulk,
    create_memory,
    search_memories,
    soft_delete_memory,
    update_memory,
)
from core_api.services.tenants import list_active_tenant_ids
from core_api.services.usage_service import bulk_check_and_increment, check_and_increment

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Memory"])


async def _stats_fallback(tenant_id: str, fleet_id: str | None) -> dict:
    """Map storage-api's ``memory_compute_health_stats`` shape onto the
    flat ``{total, by_type, by_agent, by_status}`` shape both stats
    endpoints return. ``by_agent`` is empty because storage-api doesn't
    compute it; ``partial: True`` flags the degradation so callers can
    distinguish a real empty tenant from a degraded response.

    The two stats routes return bare dicts (no ``response_model``), so
    the ``partial`` field survives FastAPI serialisation. If a
    ``response_model`` is added later, include ``partial`` in the schema
    or the degradation signal will be silently stripped.
    """
    try:
        fallback = await get_storage_client().get_memory_stats(tenant_id=tenant_id, fleet_id=fleet_id)
    except httpx.HTTPError as exc:
        # Storage-api is also down. Without this catch the httpx
        # exception would propagate through the route's ``except`` block
        # as a 500 with raw internal detail (storage-api URL, etc.).
        logger.warning(
            "memory_stats fallback: storage-api request failed",
            extra={"tenant_id": tenant_id, "fleet_id": fleet_id, "error": str(exc)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=503,
            detail="memory stats unavailable: primary DB and storage-api fallback both failed",
        ) from None
    if not fallback:
        # ``storage_client._get`` returns ``{}`` on 404 (treated as
        # absent). The httpx-error case is handled above. So an empty
        # ``fallback`` here means storage-api 404'd, but since we got
        # here only because the primary DB blew up, we can't tell a
        # real empty tenant from a storage-api hiccup. 503 surfaces the
        # cascading failure instead of returning a plausible-looking
        # ``total: 0`` to a live tenant.
        logger.warning(
            "memory_stats fallback: storage-api returned 404 or empty response "
            "(cannot distinguish empty tenant from cascading failure)",
            extra={"tenant_id": tenant_id, "fleet_id": fleet_id},
        )
        raise HTTPException(
            status_code=503,
            detail="memory stats unavailable: primary DB and storage-api fallback both failed",
        ) from None
    return {
        "total": fallback.get("total", fallback.get("total_memories", 0)),
        "by_type": fallback.get("type_distribution", {}),
        "by_agent": {},
        "by_status": fallback.get("status_distribution", {}),
        "partial": True,
    }


@router.get("/tenants")
async def list_tenants(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Return distinct tenant IDs that have memories."""
    auth.enforce_admin()
    result = await db.execute(select(Memory.tenant_id).where(Memory.deleted_at.is_(None)).distinct())
    return sorted([row[0] for row in result.all()])


@router.get("/fleets")
async def list_fleets(
    tenant_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Return distinct fleet_ids with memory counts."""
    if tenant_id:
        # Read endpoint — honors cross-tenant readable set.
        auth.enforce_readable_tenant(tenant_id)
    elif not auth.is_admin:
        # Non-admin must specify a tenant_id
        if not auth.tenant_id:
            raise HTTPException(status_code=400, detail="tenant_id is required")
        tenant_id = auth.tenant_id
    filters = [
        Memory.deleted_at.is_(None),
        Memory.fleet_id.isnot(None),
        # No ``agent_id`` accepted on this route, so there's no caller
        # identity that could legitimately see ``scope_agent`` rows.
        # Exclude them the same way ``memory_repository.list_by_filters``
        # does (line 137) and ``/memories/stats`` does. Without this,
        # ``memory_count``/``agent_count`` overstate what
        # ``GET /api/v1/memories?fleet_id=X`` would actually return.
        # ``Memory.visibility`` is ``nullable=False`` with a server
        # default (common/models/memory.py:62-66), so the SQL three-
        # valued-logic pitfall (NULL != 'scope_agent' = NULL → row
        # silently dropped) doesn't apply here.
        Memory.visibility != MEMORY_VISIBILITY_SCOPE_AGENT,
    ]
    if tenant_id:
        filters.append(Memory.tenant_id == tenant_id)
    rows = (
        await db.execute(
            select(
                Memory.fleet_id,
                func.count(),
                func.count(func.distinct(Memory.agent_id)),
            )
            .where(*filters)
            .group_by(Memory.fleet_id)
            .order_by(func.count().desc())
        )
    ).all()
    return [{"fleet_id": r[0], "memory_count": r[1], "agent_count": r[2]} for r in rows]


@router.get("/memories", response_model=PaginatedMemoryResponse)
async def list_memories(
    tenant_id: str | None = Query(default=None),
    fleet_id: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    memory_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    visibility: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    sort: str = Query(
        default="created_at",
        pattern=r"^(created_at|weight|memory_type|agent_id|status|recall_count|fleet_id|tenant_id|expires_at|deleted_at)$",
    ),
    order: str = Query(default="desc", pattern=r"^(asc|desc)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    include_deleted: bool = Query(default=False),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """List memories with filtering, sorting, and pagination.

    **Visibility scoping:** When ``agent_id`` is provided, the caller can see
    their own ``scope_agent`` memories plus all ``scope_team``/``scope_org``
    memories. When ``agent_id`` is omitted, ``scope_agent`` memories are hidden
    (safe default). This means memory types like ``insight`` that are typically
    created with agent-scoped visibility will only appear when ``agent_id`` is
    passed.

    **Pagination:** Both cursor-based and offset-based pagination are supported.
    Cursor pagination (via ``cursor``) is recommended for large datasets and only
    works with ``sort=created_at&order=desc``. Offset pagination works with any
    sort order.
    """
    if tenant_id:
        auth.enforce_readable_tenant(tenant_id)
    elif not auth.is_admin:
        if not auth.tenant_id:
            raise HTTPException(status_code=400, detail="tenant_id is required")
        tenant_id = auth.tenant_id
    if auth.tenant_id and tenant_id and agent_id and fleet_id:
        await enforce_fleet_read(db, tenant_id, agent_id, fleet_id)

    # Cursor-based pagination (only applies to created_at descending sort)
    if cursor and (sort != "created_at" or order != "desc"):
        raise HTTPException(
            status_code=400,
            detail="Cursor pagination is only supported with sort=created_at and order=desc",
        )
    c_ts = c_id = None
    if cursor:
        try:
            c_ts, c_id = decode_cursor(cursor)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid cursor")

    from core_api.repositories import memory_repo as _repo

    # `agent_id` in the REST route is both the author filter AND the
    # visibility-scoping identity. When present, the caller can see their
    # own scope_agent memories. When absent, scope_agent memories are
    # hidden (safe default — fixes the scope_agent visibility gap).
    rows = await _repo.list_by_filters(
        db,
        tenant_id=tenant_id or "",
        caller_agent_id=agent_id,  # visibility scoping
        fleet_id=fleet_id,
        written_by=agent_id,  # author filter (same param)
        memory_type=memory_type,
        status=status,
        run_id=run_id,
        include_deleted=include_deleted,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
        cursor_ts=c_ts,
        cursor_id=c_id,
    )

    has_more = len(rows) > limit
    items = [_memory_to_out(m) for m in rows[:limit]]

    next_cursor = None
    if has_more and items:
        last = rows[limit - 1]
        next_cursor = encode_cursor(last.created_at, last.id)

    return PaginatedMemoryResponse(items=items, next_cursor=next_cursor)


@router.get("/memories/stats")
async def memory_stats(
    tenant_id: str | None = Query(default=None),
    fleet_id: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    memory_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    if tenant_id:
        auth.enforce_readable_tenant(tenant_id)
    elif not auth.is_admin:
        if not auth.tenant_id:
            raise HTTPException(status_code=400, detail="tenant_id is required")
        tenant_id = auth.tenant_id

    try:
        from core_api.services.memory_stats import compute_memory_stats

        return await compute_memory_stats(
            db,
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            agent_id=agent_id,
            memory_type=memory_type,
            status=status,
            include_deleted=include_deleted,
        )
    except (OperationalError, SQLATimeoutError):
        # Connection pool exhaustion / connection drop / per-query timeout
        # are the transient failure modes worth degrading for. Programming
        # errors (ProgrammingError, IntegrityError, DataError) keep
        # bubbling so they surface as 500s. ``warning`` not ``exception``
        # so a sustained pool-exhaustion event doesn't flood logs with
        # full tracebacks at ERROR; ``exc_info=True`` keeps the traceback
        # for diagnosis. Fallback can only honour tenant_id+fleet_id (the
        # filters storage-api accepts) and never returns the soft-deleted
        # count; if the caller asked for an agent_id/memory_type/status
        # subset or include_deleted=True, returning a degraded answer
        # would lie to them — re-raise instead.
        if not tenant_id or agent_id or memory_type or status or include_deleted:
            # ``OperationalError`` / ``SQLATimeoutError`` are transient
            # pool-exhaustion / connection-drop / per-query timeout
            # conditions — surface as 503 so load balancers and clients
            # treat them as retryable. A bare ``raise`` would default
            # to 500 and trip alerting rules sized for programming
            # bugs.
            logger.warning(
                "memory_stats: direct DB query failed; raising 503 "
                "(unsupported filters or missing tenant_id)",
                exc_info=True,
            )
            raise HTTPException(
                status_code=503,
                detail="memory stats unavailable: database connection failed",
            ) from None
        logger.warning(
            "memory_stats: direct DB query failed; falling back to storage-api",
            exc_info=True,
        )
        return await _stats_fallback(tenant_id, fleet_id)


@router.delete("/memories", status_code=204)
async def delete_all_memories(
    tenant_id: str = Query(...),
    fleet_id: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    memory_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    body: dict | None = Body(default=None),
):
    """Soft-delete all matching memories for a tenant.

    Body fields (all optional):

    * ``exclude_ids``: list of UUID strings to skip from the soft-delete.
    * ``metadata_filter``: dict of equality matches against
      ``metadata->>key``. Lets clients (most concretely the load-test
      harness, tagging rows with ``metadata.load_test_run_id``) clean
      up by tag in one round-trip instead of paginated enumerate +
      per-row delete. All entries combine with AND.
    """
    auth.enforce_read_only()
    auth.enforce_tenant(tenant_id)
    from sqlalchemy import update

    stmt = update(Memory).where(Memory.tenant_id == tenant_id, Memory.deleted_at.is_(None))
    if fleet_id:
        stmt = stmt.where(Memory.fleet_id == fleet_id)
    if agent_id:
        stmt = stmt.where(Memory.agent_id == agent_id)
    if memory_type:
        stmt = stmt.where(Memory.memory_type == memory_type)
    if status:
        stmt = stmt.where(Memory.status == status)
    exclude_ids = (body or {}).get("exclude_ids", [])
    if exclude_ids:
        stmt = stmt.where(Memory.id.notin_([UUID(i) for i in exclude_ids]))
    metadata_filter = (body or {}).get("metadata_filter") or {}
    if metadata_filter:
        if not isinstance(metadata_filter, dict):
            raise HTTPException(
                status_code=400,
                detail="metadata_filter must be an object of {key: value} equality matches",
            )
        if len(metadata_filter) > 20:
            raise HTTPException(
                status_code=400,
                detail="metadata_filter supports at most 20 key/value pairs",
            )
        for key, value in metadata_filter.items():
            # PG ``metadata->>'key'`` returns JSON text — ``"true"`` for
            # booleans, ``"null"`` for None, ``'{"k":"v"}'`` for nested
            # objects. Comparing against Python ``str(value)`` would
            # produce the repr (``"True"``, ``"None"``, ``"{'k': 'v'}"``)
            # and silently match nothing. Reject non-string values
            # outright so the caller learns immediately rather than
            # debugging an empty result set.
            if not isinstance(value, str):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "metadata_filter values must be strings "
                        f"(got {type(value).__name__!r} for key {key!r})"
                    ),
                )
            # ``Memory.metadata_`` maps to the JSONB ``metadata`` column.
            # ``[key].astext == value`` compiles to PG ``metadata->>'key' = 'value'``.
            stmt = stmt.where(Memory.metadata_[str(key)].astext == value)
    stmt = stmt.values(deleted_at=datetime.now(UTC), status="deleted")
    result = await db.execute(stmt)
    deleted_count = result.rowcount
    await log_action(
        db,
        tenant_id=tenant_id,
        action="bulk_delete",
        resource_type="memory",
        detail={
            "count": deleted_count,
            "fleet_id": fleet_id,
            "agent_id": agent_id,
            "memory_type": memory_type,
            "status_filter": status,
            "metadata_filter": metadata_filter or None,
        },
    )
    await db.commit()


@router.post("/memories/bulk-delete")
async def bulk_delete_by_ids(
    body: dict = Body(...),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete memories by a list of IDs."""
    tenant_id = body.get("tenant_id")
    ids = body.get("ids", [])
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id required")
    auth.enforce_read_only()
    auth.enforce_tenant(tenant_id)
    if not ids or len(ids) > 1000:
        raise HTTPException(status_code=400, detail="ids must be 1-1000 items")

    stmt = (
        update(Memory)
        .where(
            Memory.tenant_id == tenant_id,
            Memory.id.in_([UUID(i) for i in ids]),
            Memory.deleted_at.is_(None),
        )
        .values(deleted_at=datetime.now(UTC), status="deleted")
    )
    result = await db.execute(stmt)
    await log_action(
        db,
        tenant_id=tenant_id,
        action="bulk_delete",
        resource_type="memory",
        detail={"count": result.rowcount, "method": "by_ids"},
    )
    await db.commit()
    return {"deleted": result.rowcount}


@router.get("/memories/{memory_id}")
async def get_memory(
    memory_id: UUID,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Get a single memory with full details including embedding and entity links."""
    from fastapi.responses import JSONResponse

    from common.models.entity import Entity, MemoryEntityLink

    auth.enforce_readable_tenant(tenant_id)

    t_start = time.perf_counter()
    hit = False
    error = False
    try:
        memory = await db.get(Memory, memory_id)
        if not memory or memory.tenant_id != tenant_id or memory.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Memory not found")

        # Get entity links with entity details
        entity_links = []
        try:
            links_result = await db.execute(
                select(MemoryEntityLink, Entity)
                .outerjoin(Entity, MemoryEntityLink.entity_id == Entity.id)
                .where(MemoryEntityLink.memory_id == memory_id)
            )
            for row in links_result.all():
                link, entity = row
                entry = {"entity_id": str(link.entity_id), "role": link.role}
                if entity:
                    entry["entity_type"] = entity.entity_type
                    entry["canonical_name"] = entity.canonical_name
                    entry["attributes"] = entity.attributes
                entity_links.append(entry)
        except (SQLAlchemyError, ValueError) as e:
            logger.warning("Failed to fetch entity links for memory %s: %s", memory_id, e)

        # Embedding stats
        embedding_preview = None
        embedding_stats = None
        try:
            if memory.embedding is not None:
                vec = [float(v) for v in memory.embedding]
                embedding_preview = vec[:20]
                embedding_stats = {
                    "dimensions": len(vec),
                    "min": round(min(vec), 6),
                    "max": round(max(vec), 6),
                    "mean": round(sum(vec) / len(vec), 6),
                    "non_zero": sum(1 for v in vec if abs(v) > 1e-8),
                }
        except (ValueError, TypeError) as e:
            logger.warning("Failed to compute embedding stats for memory %s: %s", memory_id, e)

        hit = True
    except HTTPException:
        # 404 (and any other HTTPException) is an expected outcome — not an
        # observability-level error. `hit=False` already signals the miss.
        raise
    except Exception:
        error = True
        raise
    finally:
        if logger.isEnabledFor(logging.INFO):
            logger.info(
                "memory-get request completed",
                extra={
                    "path": "memory-get",
                    "tenant_id": tenant_id,
                    "total_ms": (time.perf_counter() - t_start) * 1000,
                    "hit": hit,
                    "error": error,
                },
            )

    return JSONResponse(
        {
            "id": str(memory.id),
            "tenant_id": memory.tenant_id,
            "fleet_id": memory.fleet_id,
            "agent_id": memory.agent_id,
            "memory_type": memory.memory_type,
            "title": memory.title,
            "content": memory.content,
            "weight": float(memory.weight),
            "source_uri": memory.source_uri,
            "run_id": memory.run_id,
            "metadata": memory.metadata_,
            "content_hash": memory.content_hash,
            "created_at": memory.created_at.isoformat() if memory.created_at else None,
            "expires_at": memory.expires_at.isoformat() if memory.expires_at else None,
            "deleted_at": memory.deleted_at.isoformat() if memory.deleted_at else None,
            "subject_entity_id": str(memory.subject_entity_id) if memory.subject_entity_id else None,
            "predicate": memory.predicate,
            "object_value": memory.object_value,
            "ts_valid_start": memory.ts_valid_start.isoformat() if memory.ts_valid_start else None,
            "ts_valid_end": memory.ts_valid_end.isoformat() if memory.ts_valid_end else None,
            "status": memory.status,
            "visibility": memory.visibility,
            "recall_count": memory.recall_count,
            "last_recalled_at": memory.last_recalled_at.isoformat() if memory.last_recalled_at else None,
            "supersedes_id": str(memory.supersedes_id) if memory.supersedes_id else None,
            "entity_links": entity_links,
            "embedding_preview": embedding_preview,
            "embedding_stats": embedding_stats,
        }
    )


@router.get("/memories/{memory_id}/contradictions")
async def get_contradictions(
    memory_id: UUID,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Return contradiction detector findings for this memory.

    The contradiction detector (services/contradiction_detector.py) runs
    fire-and-forget post-commit on writes and persists its findings via
    two side-effects on the memories table:

    1. Conflicted/outdated rows get ``status`` set to ``"conflicted"`` or
       ``"outdated"``.
    2. The newer (winning) row's ``supersedes_id`` is set to the older
       (losing) row's id.

    There is no separate ``contradictions`` table — the supersedes chain
    + status fields ARE the persisted detector output (CAURA-604).

    Response fields (``superseded_by``, ``superseded_memories``) are kept
    intact for back-compat. New fields added in CAURA-604:

    - ``detection_status``: ``"completed"`` if any contradiction evidence
      exists for this memory (it has ``supersedes_id`` set, or another
      row supersedes it, or its own status is outdated/conflicted),
      otherwise ``"pending"``. ``"pending"`` means "no contradictions
      have been recorded" — it covers both "detector ran and found
      nothing" and "detector hasn't run yet"; the API has no way to
      distinguish those without re-running detection synchronously,
      which would side-effect from a GET. We chose the cheap, no-side-
      effect path (return pending) over an inline re-run.
    - ``contradictions``: detector-shaped findings derived from the
      persisted chain (id, status, reason, content_preview, direction,
      created_at). ``reason`` is inferred from the status of the
      conflicting row: ``outdated`` -> ``rdf_conflict``,
      ``conflicted`` -> ``semantic_conflict``. ``direction`` is
      ``superseded_by`` (this memory was replaced by a newer row) or
      ``supersedes`` (this memory replaced an older row).
    """
    auth.enforce_readable_tenant(tenant_id)

    memory = await db.get(Memory, memory_id)
    if not memory or memory.tenant_id != tenant_id or memory.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Memory not found")

    # Newer rows that point at this one as the row they replace
    # (i.e. detector found a contradiction and this memory was the
    # losing side). These are the "supersessors".
    stmt = (
        select(Memory)
        .where(
            Memory.supersedes_id == memory_id,
            Memory.tenant_id == tenant_id,
            Memory.deleted_at.is_(None),
        )
        .order_by(Memory.created_at.desc())
    )
    result = await db.execute(stmt)
    supersessors = result.scalars().all()

    # The older row this memory replaced (if any). Despite the field
    # name, ``memory.supersedes_id`` points at the OLDER memory (the
    # detector's loser); this memory was the winner. The variable name
    # ``superseded_by`` in the response is preserved as-is for
    # back-compat; ``contradictions`` below uses unambiguous direction
    # labels.
    superseded_by = None
    older = None
    if memory.supersedes_id:
        older = await db.get(Memory, memory.supersedes_id)
        # ``db.get`` is a bare PK lookup — guard against a corrupted
        # cross-tenant ``supersedes_id`` leaking another tenant's content
        # into ``superseded_by`` / ``contradictions``.
        if older and older.tenant_id != tenant_id:
            older = None
        if older and older.deleted_at is None:
            superseded_by = {
                "id": str(older.id),
                "content_preview": older.content[:200],
                "status": older.status,
                "created_at": older.created_at.isoformat() if older.created_at else None,
            }

    def _reason_for(status: str | None) -> str:
        if status == "outdated":
            return "rdf_conflict"
        if status == "conflicted":
            return "semantic_conflict"
        return "unknown"

    contradictions: list[dict] = []
    # Direction "superseded_by": newer rows that supersede THIS memory.
    # ``reason`` derives from ``memory.status`` (the loser's status carries
    # the conflict label), with ``m.status`` as fallback for the async-lag
    # window where the supersessor exists but ``memory.status`` hasn't been
    # updated yet.
    for m in supersessors:
        primary = _reason_for(memory.status)
        reason = primary if primary != "unknown" else _reason_for(m.status)
        contradictions.append(
            {
                "memory_id": str(m.id),
                "status": m.status,
                "reason": reason,
                "content_preview": m.content[:200],
                "direction": "superseded_by",
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
        )
    # Direction "supersedes": the older row THIS memory replaced.
    if older is not None and older.deleted_at is None:
        contradictions.append(
            {
                "memory_id": str(older.id),
                "status": older.status,
                "reason": _reason_for(older.status),
                "content_preview": older.content[:200],
                "direction": "supersedes",
                "created_at": older.created_at.isoformat() if older.created_at else None,
            }
        )

    # ``detection_status="completed"`` iff the response carries actionable
    # contradiction evidence. ``contradictions`` is appended above for both
    # supersessor rows AND the older row (when ``older`` survived the
    # cross-tenant + soft-deleted guards), so ``bool(contradictions)`` is
    # the single authoritative signal. The earlier ``memory.status`` and
    # ``supersedes_id`` arms could fire when every related row was excluded
    # from ``contradictions``, yielding the ``{completed, []}`` ambiguous
    # state — collapse to the single check.
    detection_status = "completed" if contradictions else "pending"

    return {
        "memory_id": str(memory_id),
        "status": memory.status,
        "superseded_by": superseded_by,
        "superseded_memories": [
            {
                "id": str(m.id),
                "content_preview": m.content[:200],
                "status": m.status,
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in supersessors
        ],
        # CAURA-604: detector-result fields. See docstring above for semantics.
        "detection_status": detection_status,
        "contradictions": contradictions,
    }


@router.post("/memories", response_model=MemoryOut, status_code=201)
@write_limit
async def write_memory(
    request: Request,
    body: MemoryCreate,
    response: Response,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(None, alias=IDEMPOTENCY_HEADER),
):
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    auth.enforce_tenant(body.tenant_id)
    # Idempotency replay is short-circuited BEFORE the per-tenant slot —
    # a cached retry must not consume a write-concurrency slot, or a
    # tenant retry storm starves its own legitimate new writes.
    # ``idempotency_for`` handles transport-prefixing (``header:`` /
    # ``body:``) and length validation; each transport gets a disjoint
    # cache namespace.
    if idempotency_key:
        _idem = await idempotency_for(request, body.tenant_id, idempotency_key, source="header")
    else:
        _idem = await idempotency_for(
            request,
            body.tenant_id,
            idempotency_key_from_metadata(body.metadata),
            source="body",
        )
    if _idem and (_replay := _idem.cached_replay):
        _body, _status = _replay
        # Replays bypass the per-tenant slot AND the
        # ``check_and_increment`` quota call below, so ``response.headers``
        # is empty here — emit no rate-limit headers rather than
        # passing empties. Acceptable trade-off: a replayed request
        # didn't consume quota, so there's no fresh ``remaining`` value
        # to publish.
        return JSONResponse(content=_body, status_code=_status)
    # Per-tenant in-flight cap fails fast with 429 if a single tenant is
    # already saturating its slot budget on this instance, instead of
    # queueing requests until they time out at the worker layer. Only
    # the new-write path is gated; replays returned above bypass it.
    async with per_tenant_slot("write", body.tenant_id):
        return await _write_memory_inner(body, response, auth, db, _idem)


async def _write_memory_inner(
    body: MemoryCreate,
    response: Response,
    auth: AuthContext,
    db: AsyncSession,
    idem: IdempotencyGuard | None,
):
    from core_api.services.organization_settings import resolve_config

    write_config = await resolve_config(db, body.tenant_id)
    agent = await get_or_create_agent(
        db,
        body.tenant_id,
        body.agent_id,
        body.fleet_id,
        require_approval=write_config.require_agent_approval,
    )
    if agent.get("trust_level", 0) == 0:
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{body.agent_id}' is not approved. Contact tenant admin to set trust_level >= 1.",
        )
    # Resolve fleet_id from agent's home fleet if not provided
    if not body.fleet_id and agent.get("fleet_id"):
        body.fleet_id = agent["fleet_id"]
    usage = None
    if auth.tenant_id:  # skip enforcement + metering for admin
        await enforce_fleet_write(db, body.tenant_id, body.agent_id, body.fleet_id)
        usage = await check_and_increment(db, body.tenant_id, "write")
    if usage:
        response.headers["X-RateLimit-Limit"] = str(usage.get("limit", "unlimited"))
        response.headers["X-RateLimit-Remaining"] = str(usage.get("remaining", "unlimited"))
    result = await create_memory(db, body)
    # STM writes return STMWriteResponse (different shape from MemoryOut)
    if isinstance(result, STMWriteResponse):
        stm_body = result.model_dump(mode="json")
        if idem:
            await idem.record(stm_body, 201)
        return JSONResponse(content=stm_body, status_code=201)
    if usage:
        result.usage = UsageSummary(
            writes_remaining=usage.get("remaining"),
            memories_limit=usage.get("limit"),
        )
    # Cache AFTER attaching usage so replays return a structurally
    # identical response. The usage snapshot is stale in the replay
    # (bounded by idempotency_ttl_seconds, default 24h) — callers who
    # need live quota should consult a fresh endpoint, not a replay.
    if idem:
        await idem.record(result.model_dump(mode="json"), 201)
    return result


BULK_ATTEMPT_ID_HEADER = "X-Bulk-Attempt-Id"
# Tighter than ``MAX_IDEMPOTENCY_KEY_LEN`` (255) because the token is
# concatenated with ``:{i}`` to form the per-row ``client_request_id``
# stored on every memory; 128 leaves comfortable room for a 100-item
# batch (``:99`` adds 3 chars) while keeping the partial-unique index
# leaves narrow.
MAX_BULK_ATTEMPT_ID_LEN = 128
_BULK_ATTEMPT_ID_PATTERN = re.compile(rf"^[A-Za-z0-9._:\-]{{1,{MAX_BULK_ATTEMPT_ID_LEN}}}$")


@router.post(
    "/memories/bulk",
    status_code=200,
    # The route returns ``JSONResponse`` directly so it can vary the
    # status code between 200 and 207, which strips FastAPI's automatic
    # response-model inference. Declare both shapes explicitly so the
    # OpenAPI doc keeps documenting the body for SDK generators.
    responses={
        200: {"model": BulkMemoryResponse},
        207: {"model": BulkMemoryResponse},
    },
)
@write_bulk_limit
async def write_memories_bulk(
    request: Request,
    body: BulkMemoryCreate,
    response: Response,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
    idempotency_key: str | None = Header(None, alias=IDEMPOTENCY_HEADER),
    bulk_attempt_id: str | None = Header(None, alias=BULK_ATTEMPT_ID_HEADER),
):
    """Write up to 100 memories with per-attempt idempotency (CAURA-602).

    Required header ``X-Bulk-Attempt-Id`` identifies the *attempt*. A
    retry of the same logical batch reuses the same value; storage's
    per-item unique constraint then converts each row into either
    ``created`` or ``duplicate_attempt`` deterministically. The route
    returns:

    - ``200`` when every item resolved as ``created``,
      ``duplicate_attempt``, or ``duplicate_content``.
    - ``207 Multi-Status`` when at least one item has ``status="error"``
      (validation, enrichment timeout, missing storage id) AND at least
      one item succeeded — partial outcomes are explicit, never inferred
      from a 5xx with side effects.
    - ``200`` when every item is an error too — the request was processed
      successfully; per-item ``status`` carries the rejection. We
      deliberately don't reuse 422 here because FastAPI already emits
      422 for request-body Pydantic validation, and clients that branch
      on status code alone shouldn't have to disambiguate "request was
      malformed" from "request parsed and every item was rejected on
      merit."
    - ``504`` when the bulk-only budget burns before storage commits;
      the response carries no per-item state, so a retry with the same
      attempt id is the recovery path.

    The pre-existing ``Idempotency-Key`` header still controls
    *response*-level replay; ``X-Bulk-Attempt-Id`` is a separate
    contract operating one layer down. Both are honoured: the header
    cache short-circuits when the receipt is final, the per-row
    constraint resolves the slow path when the receipt is missing or
    pending.
    """
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    auth.enforce_tenant(body.tenant_id)

    # Broker (mci_ install credential) calls don't carry an attempt id
    # header or an ``agent_id`` body field — the broker's own per-
    # session ``client_hash`` de-dup is the design contract per
    # cloud-data-plane.md §2.4 (gap G3). Server-derive a per-request
    # attempt id and attribute writes to the install. Non-broker
    # callers (dashboard, SDK) keep the CAURA-602 invariants in full.
    if auth.is_install_credential:
        if not bulk_attempt_id:
            import uuid as _uuid

            bulk_attempt_id = f"broker-{auth.install_uuid or 'unknown'}-{_uuid.uuid4()}"
        if not body.agent_id:
            body.agent_id = f"broker:{auth.install_uuid or 'unknown'}"
    if not bulk_attempt_id:
        # Required as of CAURA-602 — without it we can't make the bulk
        # write retry-safe, and silent-create regressions reappear under
        # any storage-side cancellation. Reject with a 4xx so SDKs see
        # the breaking change, not a soft-fallback that masks the bug.
        raise HTTPException(
            status_code=400,
            detail=f"Missing required {BULK_ATTEMPT_ID_HEADER} header",
        )
    if not _BULK_ATTEMPT_ID_PATTERN.match(bulk_attempt_id):
        raise HTTPException(
            status_code=400,
            detail=(f"Invalid {BULK_ATTEMPT_ID_HEADER}: must match ^[A-Za-z0-9._:\\-]{{1,128}}$"),
        )

    # Idempotency replay short-circuits BEFORE the per-tenant slot — a
    # cached retry must not consume a write-concurrency slot.
    _idem = await idempotency_for(request, body.tenant_id, idempotency_key)
    if _idem and (_replay := _idem.cached_replay):
        _body, _status = _replay
        # Replays bypass the per-tenant slot and the bulk
        # quota-increment, so no rate-limit headers are available to
        # carry on the cached response.
        return JSONResponse(content=_body, status_code=_status)
    async with per_tenant_slot("write", body.tenant_id):
        return await _write_memories_bulk_inner(body, response, auth, db, _idem, bulk_attempt_id)


def _bulk_response(result: BulkMemoryResponse) -> JSONResponse:
    """Pick HTTP status per CAURA-602 contract:

    - 200 when there are no errors at all, OR every item is an error
      (the request was processed; rejections are per-item business
      logic, not a transport-level failure). Avoiding 422 here keeps
      the route's response space disjoint from FastAPI's automatic
      422-on-body-validation.
    - 207 Multi-Status when the batch is mixed — at least one error
      AND at least one resolved row. ``duplicate_attempt`` and
      ``duplicate_content`` both count as resolved (they carry an
      ``id``), so a batch of ``[error, duplicate_attempt]`` (i.e.
      ``created=0, duplicates=1, errors=1``) is 207, not 200 — the
      condition tests ``created == 0 and duplicates == 0`` *together*
      so any duplicate keeps the response in the mixed bucket.

    Always returns the body verbatim; per-item ``status`` carries the
    real outcome, so the HTTP code is purely a hint for clients that
    can branch on 2xx / 207.
    """
    if result.errors == 0 or (result.created == 0 and result.duplicates == 0):
        status_code = 200
    else:
        status_code = 207
    return JSONResponse(content=result.model_dump(mode="json"), status_code=status_code)


async def _write_memories_bulk_inner(
    body: BulkMemoryCreate,
    response: Response,
    auth: AuthContext,
    db: AsyncSession,
    idem: IdempotencyGuard | None,
    bulk_attempt_id: str,
):
    agent = await get_or_create_agent(db, body.tenant_id, body.agent_id, body.fleet_id)
    if not body.fleet_id and agent.get("fleet_id"):
        body.fleet_id = agent["fleet_id"]
    usage = None
    if auth.tenant_id:  # skip enforcement + metering for admin
        await enforce_fleet_write(db, body.tenant_id, body.agent_id, body.fleet_id)
        usage = await bulk_check_and_increment(db, body.tenant_id, len(body.items))
    if usage:
        response.headers["X-RateLimit-Limit"] = str(usage.get("limit", "unlimited"))
        response.headers["X-RateLimit-Remaining"] = str(usage.get("remaining", "unlimited"))

    try:
        result = await asyncio.wait_for(
            create_memories_bulk(db, body, bulk_attempt_id=bulk_attempt_id),
            timeout=app_settings.bulk_request_timeout_seconds,
        )
    except (TimeoutError, httpx.TimeoutException):
        # ``asyncio.wait_for`` documents raising ``asyncio.TimeoutError``,
        # which Python 3.11 aliased to the builtin ``TimeoutError``.
        # Project floor is 3.12 (``requires-python = ">=3.12"``), so the
        # builtin form is correct everywhere we run; ruff UP041 also
        # mandates this shape. Anyone porting back to Python 3.10 must
        # change this to ``except asyncio.TimeoutError:`` first.
        #
        # ``httpx.TimeoutException`` is the base for ReadTimeout /
        # WriteTimeout / ConnectTimeout / PoolTimeout. The storage
        # client's pool sets ``read=120s`` deliberately above the 90s
        # ``bulk_request_timeout_seconds`` so the asyncio cancellation
        # almost always wins the race, but we catch httpx timeouts here
        # as a defence-in-depth fallback. Either path means the storage
        # call may have committed without surfacing an id; the
        # ``X-Bulk-Attempt-Id`` retry contract recovers it.
        #
        # The per-phase ``storage_bulk_timeout_seconds`` deadline (raised
        # from ``asyncio.timeout`` inside ``create_memories_bulk``) also
        # lands here as a plain ``TimeoutError`` — same recovery contract.
        # Both caps are logged as static config values so the access-log
        # entry is self-explanatory; the actual elapsed time on the
        # request line distinguishes which timer fired (storage cap at
        # ~25s elapsed vs umbrella at ~90s elapsed).
        logger.warning(
            "bulk write timed out (storage cap %ss / request cap %ss); client should retry with same %s",
            app_settings.storage_bulk_timeout_seconds,
            app_settings.bulk_request_timeout_seconds,
            BULK_ATTEMPT_ID_HEADER,
        )
        # No per-item state to surface — the storage call may have
        # committed some rows, none, or be still in flight. Do NOT
        # record the idempotency receipt so a retry doesn't replay an
        # incomplete answer; the per-item attempt-id is the recovery
        # contract and a retry will resolve every committed row to
        # ``duplicate_attempt`` with its canonical id.
        raise HTTPException(
            status_code=504,
            detail=(
                "bulk write timed out before completing; retry with "
                f"the same {BULK_ATTEMPT_ID_HEADER} to recover any "
                "committed items."
            ),
        )
    except httpx.HTTPStatusError as exc:
        # Storage 5xx (raised by ``resp.raise_for_status()`` in the storage
        # client) may have committed rows before failing — same shape as
        # the timeout branch — so map to 504 to keep the
        # ``X-Bulk-Attempt-Id`` retry contract intact. 4xx escapes here
        # intentionally: it signals a request-shape problem the client
        # must fix, not transient noise the retry path can recover from.
        if not (500 <= exc.response.status_code < 600):
            # Log so the 4xx path is observable in structured logs;
            # without this, only FastAPI's generic unhandled-exception
            # log surfaces and it has no bulk context.
            logger.warning(
                "bulk write got unexpected %s from storage; this is a request-shape bug",
                exc.response.status_code,
                exc_info=True,
            )
            raise
        logger.warning(
            "bulk write got %s from storage; client should retry with same %s",
            exc.response.status_code,
            BULK_ATTEMPT_ID_HEADER,
        )
        raise HTTPException(
            status_code=504,
            detail=(
                "bulk write upstream error before completing; retry with "
                f"the same {BULK_ATTEMPT_ID_HEADER} to recover any "
                "committed items."
            ),
        )
    except httpx.RequestError:
        # Network-level error reaching storage (DNS failure, connect
        # refused, broken pipe). Note that ``httpx.PoolTimeout`` IS a
        # ``TimeoutException`` and is already handled by the earlier
        # ``except (TimeoutError, httpx.TimeoutException)`` clause —
        # it never reaches this branch.
        #
        # Surface as 503 with ``Retry-After`` so the client can back off
        # cleanly. Same recovery shape as the timeout/5xx branches: no
        # idempotency receipt, attempt-id resolves any committed rows.
        logger.warning(
            "bulk write got network error reaching storage; client should retry with same %s",
            BULK_ATTEMPT_ID_HEADER,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "bulk write upstream unreachable; retry with the same "
                f"{BULK_ATTEMPT_ID_HEADER} to recover any committed items."
            ),
            headers={
                "Retry-After": str(app_settings.storage_network_error_retry_after_seconds),
            },
        )

    bulk_resp = _bulk_response(result)
    if idem:
        # Replay the live status code, not a hardcoded 200 — a 207
        # batch with mixed errors must replay AS 207, not as a 200
        # that hides the partial failure from the retried client.
        await idem.record(result.model_dump(mode="json"), bulk_resp.status_code)
    return bulk_resp


@router.delete("/memories/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: UUID,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    auth.enforce_read_only()
    auth.enforce_tenant(tenant_id)
    if auth.tenant_id and agent_id:
        await enforce_delete(db, tenant_id, agent_id)
    await soft_delete_memory(db, memory_id, tenant_id)
    await log_action(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        action="delete",
        resource_type="memory",
        resource_id=memory_id,
    )
    await db.commit()


@router.patch("/memories/{memory_id}/status")
async def update_memory_status(
    memory_id: UUID,
    body: dict,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Update memory status (e.g., active → confirmed)."""
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    auth.enforce_tenant(tenant_id)
    from core_api.constants import MEMORY_STATUSES

    status = body.get("status")
    if status not in MEMORY_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {', '.join(MEMORY_STATUSES)}",
        )
    memory = await db.get(Memory, memory_id)
    if not memory or memory.tenant_id != tenant_id or memory.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Memory not found")
    old_status = memory.status
    memory.status = status

    await log_action(
        db,
        tenant_id=tenant_id,
        agent_id=memory.agent_id,
        action="status_update",
        resource_type="memory",
        resource_id=memory_id,
        detail={"old_status": old_status, "new_status": status},
    )
    await db.commit()
    return {"memory_id": str(memory_id), "old_status": old_status, "new_status": status}


@router.patch("/memories/{memory_id}", response_model=MemoryOut)
async def update_memory_endpoint(
    memory_id: UUID,
    body: MemoryUpdate,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Update a memory. Only provide fields you want to change.
    If content changes, embedding and entity extraction are re-run automatically."""
    auth.enforce_tenant(tenant_id)
    if auth.tenant_id:  # skip usage metering for admin
        await check_and_increment(db, tenant_id, "write")
    return await update_memory(
        db,
        memory_id,
        tenant_id,
        body,
        agent_id=agent_id if auth.tenant_id else None,
    )


@router.post("/search", response_model=SearchResponse)
@search_limit
async def search(
    request: Request,
    body: SearchRequest,
    response: Response,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    # Read endpoint — honors cross-tenant readable set. The SQL
    # widens to readable_tenant_ids inside search_memories so the
    # body.tenant_id can be any tenant the caller may read from
    # (typically home, but explicit source queries work too).
    auth.enforce_readable_tenant(body.tenant_id)
    async with per_tenant_slot("search", body.tenant_id):
        return await _search_inner(body, response, auth, db)


async def _search_inner(
    body: SearchRequest,
    response: Response,
    auth: AuthContext,
    db: AsyncSession,
):
    usage = None
    _agent = None
    if auth.tenant_id:  # skip for admin
        if body.filter_agent_id:
            fleet_id_hint = body.fleet_ids[0] if body.fleet_ids and len(body.fleet_ids) == 1 else None
            _agent = await get_or_create_agent(db, body.tenant_id, body.filter_agent_id, fleet_id_hint)
            if not body.fleet_ids and _agent.get("fleet_id") and _agent.get("trust_level", 0) < 2:
                body.fleet_ids = [_agent["fleet_id"]]  # Force fleet scoping for trust < 2
            if body.fleet_ids and len(body.fleet_ids) == 1:
                await enforce_fleet_read(db, body.tenant_id, body.filter_agent_id, body.fleet_ids[0])
        usage = await check_and_increment(db, body.tenant_id, "search")
    if usage:
        response.headers["X-RateLimit-Limit"] = str(usage.get("limit", "unlimited"))
        response.headers["X-RateLimit-Remaining"] = str(usage.get("remaining", "unlimited"))
    from core_api.services.organization_settings import resolve_config

    t_start = time.perf_counter()
    success = True
    results: list = []
    try:
        config = await resolve_config(db, body.tenant_id)
        # Widen the read predicate when the caller authenticated with
        # a cross-tenant key. Single-tenant keys leave
        # ``readable_tenant_ids = [tenant_id]`` so this is a no-op.
        results = await search_memories(
            db,
            tenant_id=body.tenant_id,
            query=body.query,
            fleet_ids=body.fleet_ids,
            filter_agent_id=body.filter_agent_id,
            caller_agent_id=body.filter_agent_id,
            memory_type_filter=body.memory_type_filter,
            status_filter=body.status_filter,
            valid_at=body.valid_at,
            top_k=body.top_k,
            recall_boost=config.recall_boost,
            graph_expand=config.graph_expand,
            tenant_config=config,
            search_profile=_agent.get("search_profile") if _agent else None,
            readable_tenant_ids=auth.readable_tenant_ids if auth.is_cross_tenant_read else None,
        )
    except HTTPException:
        # Auth / tenant errors raised downstream are expected outcomes,
        # not DB/network failures — don't flag them as ``error=True``.
        raise
    except Exception:
        success = False
        raise
    finally:
        if logger.isEnabledFor(logging.INFO):
            logger.info(
                "search request completed",
                extra={
                    "path": "memory-search",
                    "tenant_id": body.tenant_id,
                    "top_k": body.top_k,
                    "row_count": len(results),
                    "total_ms": (time.perf_counter() - t_start) * 1000,
                    "error": not success,
                },
            )
    return SearchResponse(items=results)


@router.post("/ingest/preview")
async def ingest_preview_endpoint(
    body: IngestRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Extract facts from a URL or text for preview (no writes).

    Body size cap: enforced upstream by ``IngestBodySizeMiddleware`` (PR #9).
    """
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    auth.enforce_tenant(body.tenant_id)
    return await ingest_preview(db, body)


@router.post("/ingest/commit")
@write_limit
async def ingest_commit_endpoint(
    request: Request,
    body: IngestCommitRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Write previewed facts as memories."""
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    auth.enforce_tenant(body.tenant_id)
    if auth.tenant_id:  # skip for admin
        await check_and_increment(db, body.tenant_id, "write")
    return await ingest_commit(db, body)


@router.post("/ingest/file")
async def ingest_file_endpoint(
    file: UploadFile = File(...),
    tenant_id: str = Form(...),
    focus: str | None = Form(None),
    fleet_id: str | None = Form(None),
    agent_id: str | None = Form(None),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """PR #9: multipart file upload entry point.

    Accepts the same 3 MB cap as JSON / URL ingest. Supported MIMEs:
    - Text formats (md, txt, csv, html): decoded directly.
    - Binary formats (pdf, docx, pptx, xlsx, epub, rtf, odt): routed
      through Kreuzberg for text extraction.

    The uploaded file's ``Content-Type`` drives dispatch. The Content-Length
    cap is enforced upstream by ``IngestBodySizeMiddleware``; we re-check
    the extracted payload size here for defense in depth (multipart envelope
    overhead can hide the real payload size from the request header).
    """
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    auth.enforce_tenant(tenant_id)

    body = await file.read()
    if len(body) > INGEST_MAX_INPUT_BYTES:
        max_mb = INGEST_MAX_INPUT_BYTES // 1_000_000
        raise HTTPException(
            status_code=413,
            detail=f"File must be {max_mb} MB or under (got {len(body):,} bytes).",
        )

    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime in BINARY_INGEST_MIME_TYPES:
        text = await _extract_with_kreuzberg(body, mime)
    elif mime in TEXT_INGEST_MIME_TYPES:
        text = decode_text_body(body, mime)
    else:
        raise HTTPException(
            status_code=422,
            detail=(f"Unsupported content type: {mime}. Allowed: {sorted(ALLOWED_INGEST_MIME_TYPES)}"),
        )

    # IngestRequest.agent_id has a non-None default ("ingest-agent"); pass
    # only when the form actually carried one to avoid clobbering the default.
    kwargs: dict = {"tenant_id": tenant_id, "content": text, "focus": focus, "fleet_id": fleet_id}
    if agent_id is not None:
        kwargs["agent_id"] = agent_id
    # Preserve the original filename as ``upload:<filename>`` so each
    # resulting memory's ``source_uri`` says where it came from instead
    # of the generic ``"text-input"`` marker. ``file.filename`` can be
    # absent / empty when the client doesn't send a multipart name, in
    # which case we fall back to a generic ``"upload"`` label.
    filename = (file.filename or "").strip() or None
    kwargs["source_uri"] = f"upload:{filename}" if filename else "upload"
    req = IngestRequest(**kwargs)
    return await ingest_preview(db, req)


@router.post("/ingest/undo/{run_id}")
async def ingest_undo_endpoint(
    run_id: str,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """A3 (PR #6): soft-delete every memory tagged with the given run_id.

    The undo lever for an entire ingest batch. Filters by the top-level
    ``Memory.run_id`` column (indexed as of the parent-doc PR) AND
    ``metadata.source = "ingest"`` (belt-and-braces — prevents non-ingest
    memories sharing a run_id from being touched) AND tenant_id ownership.

    Also deletes the parent ``documents`` row for the batch
    (``collection='ingest-sources'``, ``doc_id=<run_id>``) on a best-effort
    basis — a missing parent isn't an error since older batches predate
    the parent-doc PR.

    Returns ``{"deleted": N, "run_id": "..."}``. ``deleted=0`` is a valid
    response (no rows matched — already cleaned up or never existed).
    """
    auth.enforce_read_only()
    auth.enforce_tenant(tenant_id)

    stmt = (
        update(Memory)
        .where(
            Memory.tenant_id == tenant_id,
            Memory.deleted_at.is_(None),
            Memory.run_id == run_id,
            Memory.metadata_["source"].astext == "ingest",
        )
        .values(deleted_at=datetime.now(UTC), status="deleted")
    )
    result = await db.execute(stmt)
    deleted_count = result.rowcount

    # Delete the parent Document for this batch (introduced by the
    # parent-doc PR). Best-effort: older batches predate the parent-doc
    # write and won't have one, which is fine — undo on those still
    # soft-deletes the memories, just without a parent record to drop.
    try:
        sc = get_storage_client()
        await sc.delete_document(tenant_id=tenant_id, collection=INGEST_DOCUMENTS_COLLECTION, doc_id=run_id)
    except Exception:
        logger.info(
            "ingest_undo: parent Document delete failed or no-op (run_id=%s) — "
            "memories soft-deleted regardless",
            run_id,
            exc_info=False,
        )

    await log_action(
        db,
        tenant_id=tenant_id,
        action="ingest_undo",
        resource_type="memory",
        detail={"run_id": run_id, "count": deleted_count},
    )
    await db.commit()
    return {"deleted": deleted_count, "run_id": run_id}


@router.post("/recall")
@search_limit
async def recall_endpoint(
    request: Request,
    body: SearchRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Search memories and return an LLM-synthesized context summary."""
    # Read endpoint — readable set widening applies (see /search).
    auth.enforce_readable_tenant(body.tenant_id)
    if auth.tenant_id:
        if body.filter_agent_id:
            fleet_id_hint = body.fleet_ids[0] if body.fleet_ids and len(body.fleet_ids) == 1 else None
            _agent = await get_or_create_agent(db, body.tenant_id, body.filter_agent_id, fleet_id_hint)
            if not body.fleet_ids and _agent.get("fleet_id") and _agent.get("trust_level", 0) < 2:
                body.fleet_ids = [_agent["fleet_id"]]
            if body.fleet_ids and len(body.fleet_ids) == 1:
                await enforce_fleet_read(db, body.tenant_id, body.filter_agent_id, body.fleet_ids[0])
        await check_and_increment(db, body.tenant_id, "search")
    from core_api.services.recall_service import recall

    return await recall(
        db,
        tenant_id=body.tenant_id,
        query=body.query,
        fleet_ids=body.fleet_ids,
        filter_agent_id=body.filter_agent_id,
        memory_type_filter=body.memory_type_filter,
        status_filter=body.status_filter,
        top_k=body.top_k,
        valid_at=body.valid_at,
        readable_tenant_ids=auth.readable_tenant_ids if auth.is_cross_tenant_read else None,
    )


# ---------------------------------------------------------------------------
# Redistribute
# ---------------------------------------------------------------------------


@router.post("/memories/redistribute", response_model=RedistributeResponse)
async def redistribute_memories(
    body: RedistributeRequest,
    tenant_id: str = Query(...),
    agent_id: str = Query(..., description="Requesting agent (must be trust_level >= 3)"),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Bulk-reassign memories to a different agent.

    Admin-only (trust_level >= 3).  Moves memories to ``target_agent_id``,
    auto-promoting ``scope_agent`` → ``scope_team`` to prevent data loss.

    Cross-fleet moves are intentionally allowed — a primary use case is
    reassigning memories from a central orchestrator to domain specialists
    that may reside in different fleets.  The admin trust-level requirement
    (>= 3) is the authorization gate instead of fleet scoping.
    """
    t0 = time.perf_counter()

    auth.enforce_read_only()
    auth.enforce_usage_limits()
    auth.enforce_tenant(tenant_id)

    # Verify requesting agent is admin
    caller = await lookup_agent(db, tenant_id, agent_id)
    if caller is None or caller.get("trust_level", 0) < 3:
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{agent_id}' requires trust_level >= 3 for redistribute.",
        )

    # Verify target agent exists and is not restricted
    target = await lookup_agent(db, tenant_id, body.target_agent_id)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"Target agent '{body.target_agent_id}' not found in tenant '{tenant_id}'.",
        )
    if target.get("trust_level", 0) < 1:
        raise HTTPException(
            status_code=403,
            detail=f"Target agent '{body.target_agent_id}' is restricted (trust_level=0). "
            "Cannot assign memories to a restricted agent.",
        )

    # Usage quota
    if auth.tenant_id:  # skip usage metering for admin tokens
        await bulk_check_and_increment(db, tenant_id, len(body.memory_ids))

    # Fetch matching memories (tenant-scoped, not deleted)
    stmt = select(Memory).where(
        Memory.id.in_(body.memory_ids),
        Memory.tenant_id == tenant_id,
        Memory.deleted_at.is_(None),
    )
    result = await db.execute(stmt)
    memories = result.scalars().all()

    # Track IDs not found (deleted, wrong tenant, or non-existent)
    found_ids = {mem.id for mem in memories}
    not_found = [str(mid) for mid in body.memory_ids if mid not in found_ids]

    moved = 0
    promoted = 0
    skipped = 0
    from_agents: set[str] = set()

    for mem in memories:
        if mem.agent_id == body.target_agent_id:
            skipped += 1
            continue

        from_agents.add(mem.agent_id)
        mem.agent_id = body.target_agent_id

        # Auto-promote scope_agent to scope_team to prevent data loss
        if mem.visibility == "scope_agent":
            mem.visibility = "scope_team"
            promoted += 1

        moved += 1

    # Audit log (same transaction as memory mutations — single atomic commit)
    await log_action(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        action="redistribute",
        resource_type="memory",
        detail={
            "target_agent_id": body.target_agent_id,
            "from_agents": sorted(from_agents),
            "moved": moved,
            "promoted": promoted,
            "skipped": skipped,
            "requested": len(body.memory_ids),
        },
    )
    await db.commit()

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return RedistributeResponse(
        moved=moved,
        promoted=promoted,
        skipped=skipped,
        errors=not_found,
        redistribute_ms=elapsed_ms,
    )


# ---------------------------------------------------------------------------
# Admin global-view endpoints (requires enforce_admin)
# ---------------------------------------------------------------------------

admin_memories_router = APIRouter(tags=["Admin"])


@admin_memories_router.get("/admin/tenants")
async def admin_list_tenants(
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Admin: list all tenant IDs that have memories."""
    auth.enforce_admin()
    return await list_active_tenant_ids(db)


@admin_memories_router.get("/admin/fleets")
async def admin_list_fleets(
    tenant_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Admin: list distinct fleet_ids with memory counts."""
    auth.enforce_admin()
    filters = [Memory.deleted_at.is_(None), Memory.fleet_id.isnot(None)]
    if tenant_id:
        filters.append(Memory.tenant_id == tenant_id)
    rows = (
        await db.execute(
            select(
                Memory.fleet_id,
                func.count(),
                func.count(func.distinct(Memory.agent_id)),
            )
            .where(*filters)
            .group_by(Memory.fleet_id)
            .order_by(func.count().desc())
        )
    ).all()
    return [{"fleet_id": r[0], "memory_count": r[1], "agent_count": r[2]} for r in rows]


@admin_memories_router.get("/admin/memories", response_model=PaginatedMemoryResponse)
async def admin_list_memories(
    tenant_id: str | None = Query(default=None),
    fleet_id: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    memory_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    sort: str = Query(
        default="created_at",
        pattern=r"^(created_at|weight|memory_type|agent_id|status|recall_count|fleet_id|tenant_id|expires_at|deleted_at)$",
    ),
    order: str = Query(default="desc", pattern=r"^(asc|desc)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    include_deleted: bool = Query(default=False),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Admin: list memories across all tenants with full pagination."""
    auth.enforce_admin()
    stmt = select(Memory)
    if tenant_id:
        stmt = stmt.where(Memory.tenant_id == tenant_id)
    if fleet_id:
        stmt = stmt.where(Memory.fleet_id == fleet_id)
    if not include_deleted:
        stmt = stmt.where(Memory.deleted_at.is_(None))
    if agent_id:
        stmt = stmt.where(Memory.agent_id == agent_id)
    if memory_type:
        stmt = stmt.where(Memory.memory_type == memory_type)
    if status:
        stmt = stmt.where(Memory.status == status)

    if cursor and (sort != "created_at" or order != "desc"):
        raise HTTPException(
            status_code=400,
            detail="Cursor pagination is only supported with sort=created_at and order=desc",
        )
    if cursor:
        try:
            cursor_ts, cursor_id = decode_cursor(cursor)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid cursor")
        stmt = stmt.where(tuple_(Memory.created_at, Memory.id) < tuple_(cursor_ts, cursor_id))

    col = getattr(Memory, sort)
    stmt = stmt.order_by(*paginated_order_by(col, Memory.id, order))
    if not cursor:
        stmt = stmt.offset(offset)
    stmt = stmt.limit(limit + 1)

    result = await db.execute(stmt)
    rows = result.scalars().all()

    has_more = len(rows) > limit
    items = [_memory_to_out(m) for m in rows[:limit]]

    next_cursor = None
    if has_more and items:
        last = rows[limit - 1]
        next_cursor = encode_cursor(last.created_at, last.id)

    return PaginatedMemoryResponse(items=items, next_cursor=next_cursor)


@admin_memories_router.get("/admin/memories/stats")
async def admin_memory_stats(
    tenant_id: str | None = Query(default=None),
    fleet_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Admin: memory stats across all tenants (or filtered by tenant_id)."""
    auth.enforce_admin()
    filters = [Memory.deleted_at.is_(None)]
    if tenant_id:
        filters.append(Memory.tenant_id == tenant_id)
    if fleet_id:
        filters.append(Memory.fleet_id == fleet_id)
    base = select(Memory).where(*filters)

    try:
        total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar()
        by_type = dict(
            (
                await db.execute(
                    select(Memory.memory_type, func.count()).where(*filters).group_by(Memory.memory_type)
                )
            ).all()
        )
        by_agent = dict(
            (
                await db.execute(
                    select(Memory.agent_id, func.count()).where(*filters).group_by(Memory.agent_id)
                )
            ).all()
        )
        by_status = dict(
            (
                await db.execute(select(Memory.status, func.count()).where(*filters).group_by(Memory.status))
            ).all()
        )
        return {
            "total": total,
            "by_type": by_type,
            "by_agent": by_agent,
            "by_status": by_status,
        }
    except (OperationalError, SQLATimeoutError):
        # Same transient-only fallback as the tenant-facing /memories/stats
        # above. ``warning`` (not ``exception``) avoids log floods under
        # sustained pool exhaustion. The admin endpoint cannot pass an
        # unbounded tenant_id to storage-api (storage_client requires
        # one), so the cross-tenant case has no fallback path and
        # re-raises.
        if not tenant_id:
            logger.warning(
                "admin_memory_stats: direct DB query failed; raising 503 "
                "(cross-tenant admin call has no fallback path)",
                exc_info=True,
            )
            raise HTTPException(
                status_code=503,
                detail="memory stats unavailable: database connection failed",
            ) from None
        logger.warning(
            "admin_memory_stats: direct DB query failed; falling back to storage-api",
            exc_info=True,
        )
        return await _stats_fallback(tenant_id, fleet_id)
