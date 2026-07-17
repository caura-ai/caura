import asyncio
import logging
import re
import time
from datetime import datetime
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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from common.enrichment.constants import SERVER_RESERVED_MEMORY_TYPES
from core_api.agent_ids import DEFAULT_AGENT_ID
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.config import settings as app_settings
from core_api.constants import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
)
from core_api.middleware.idempotency import (
    IDEMPOTENCY_HEADER,
    IdempotencyGuard,
    idempotency_for,
    idempotency_key_from_metadata,
)
from core_api.middleware.per_tenant_concurrency import per_tenant_slot
from core_api.middleware.rate_limit import search_limit, write_bulk_limit, write_limit
from core_api.pagination import decode_cursor, encode_cursor
from core_api.schemas import (
    BulkMemoryCreate,
    BulkMemoryItem,
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
    authorize_memory_access,
    broker_label,
    broker_owned_agent_id,
    enforce_delete,
    enforce_fleet_read,
    enforce_fleet_write,
    get_or_create_agent,
    lookup_agent,
    resolve_write_agent,
)
from core_api.services.audit_service import log_action, log_cross_tenant_read
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


def _reject_reserved_memory_type(memory_type: str | None, *, index: int | None = None) -> None:
    """C3/C8 — reject agent-supplied reserved memory types at the API boundary.

    Reserved types (``outcome``, ``rule``, ``insight``) are emitted by the
    server's internal write paths — evolve_service for outcome/rule,
    insights_service for insight. Agents writing them explicitly creates
    rows that downstream queries treat as system-authored, polluting
    insights / RL signals. Internal callers go through
    ``services.memory_service.create_memory`` directly and bypass this
    check; only the REST + MCP entry points are gated.

    ``index`` names the offending row in a bulk request so the operator
    can find it; absent on the single-write path.
    """
    if memory_type is None or memory_type not in SERVER_RESERVED_MEMORY_TYPES:
        return
    # ``memory_type`` is a ``MemoryType`` str-Enum value; ``!r`` would render
    # ``<MemoryType.INSIGHT: 'insight'>`` which leaks the wrapper into the
    # operator-facing message. ``.value`` (when present) gives the raw slug
    # the API actually accepts; plain ``str(...)`` is the fallback.
    slug = memory_type.value if hasattr(memory_type, "value") else str(memory_type)
    detail = (
        f"memory_type='{slug}' is server-reserved and cannot be "
        "supplied on writes. Use memclaw_evolve for outcome/rule or "
        "memclaw_insights for insight; for agent-authored reflections, "
        "use memory_type='fact' (or omit memory_type to auto-classify)."
    )
    if index is not None:
        detail = f"items[{index}]: {detail}"
    raise HTTPException(status_code=422, detail=detail)


def _missing_agent_id_error() -> RequestValidationError:
    """Build the 422 raised when a non-standalone write omits ``agent_id``.

    Mirrors the shape FastAPI produces for a missing required field, so the
    app's validation-envelope handler renders it as a 422 INVALID_ARGUMENTS.
    RequestValidationError stores errors verbatim and ``.errors()`` returns
    them as-is, so a pre-rendered dict is the supported input; the handler
    only reads ``msg`` and runs the list through ``jsonable_encoder``.
    """
    return RequestValidationError(
        errors=[
            {
                "type": "missing",
                "loc": ("body", "agent_id"),
                "msg": ("agent_id is required; only the standalone single-tenant deployment may omit it."),
                "input": None,
            }
        ]
    )


@router.get("/tenants")
async def list_tenants(
    auth: AuthContext = Depends(get_auth_context),
):
    """Return distinct tenant IDs that have memories."""
    auth.enforce_admin()
    return sorted(await get_storage_client().list_active_tenants())


@router.get("/fleets")
async def list_fleets(
    tenant_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
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
    # No ``agent_id`` accepted on this route, so there's no caller identity
    # that could legitimately see ``scope_agent`` rows — exclude them the same
    # way ``memory_repository.list_by_filters`` and ``/memories/stats`` do, so
    # the counts don't overstate what ``GET /api/v1/memories?fleet_id=X`` would
    # actually return. The storage endpoint applies this exclusion server-side
    # when ``exclude_scope_agent=True``.
    return await get_storage_client().memory_fleet_distribution(tenant_id, exclude_scope_agent=True)


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
    # Capture whether the caller explicitly pinned tenant_id BEFORE we
    # default it to home below. This drives the cross-tenant widening
    # decision: pinned → single-tenant scope; omitted + cross-tenant
    # credential → widen across the readable set.
    tenant_id_explicit = bool(tenant_id)
    if tenant_id:
        auth.enforce_readable_tenant(tenant_id)
    elif not auth.is_admin:
        if not auth.tenant_id:
            raise HTTPException(status_code=400, detail="tenant_id is required")
        tenant_id = auth.tenant_id
    # Visibility/fleet identity: prefer the gateway-authenticated agent over the
    # caller-supplied query param so an agent credential can't widen its view by
    # passing a peer's agent_id (which would expose that peer's scope_agent rows)
    # or by omitting it. The query param stays the AUTHOR filter (written_by).
    # A tenant/user credential (auth.agent_id None) keeps using the param, as the
    # dashboard intends.
    caller_agent_id = auth.agent_id or agent_id
    if auth.tenant_id and tenant_id and caller_agent_id and fleet_id:
        await enforce_fleet_read(tenant_id, caller_agent_id, fleet_id)

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

    # `agent_id` in the REST route is both the author filter AND the
    # visibility-scoping identity. When present, the caller can see their
    # own scope_agent memories. When absent, scope_agent memories are
    # hidden (safe default — fixes the scope_agent visibility gap).
    # Cross-tenant widening: when the caller's credential carries a
    # readable set wider than home AND didn't pin tenant_id, storage
    # widens to ``tenant_id = ANY($readable)``. Pinning to one tenant
    # keeps the result scoped (the gate above already verified access).
    # Routed through core-storage-api (same visibility predicate + cursor
    # + readable-set widening the MCP list path uses).
    list_payload: dict = {
        "tenant_id": tenant_id or "",
        "caller_agent_id": caller_agent_id,  # visibility scoping (authenticated identity)
        "fleet_id": fleet_id,
        "written_by": agent_id,  # author filter (query param)
        "memory_type": memory_type,
        "status": status,
        "run_id": run_id,
        "include_deleted": include_deleted,
        "sort": sort,
        "order": order,
        "limit": limit,
        "offset": offset,
        "cursor_ts": c_ts.isoformat() if c_ts else None,
        "cursor_id": str(c_id) if c_id else None,
        "readable_tenant_ids": (
            auth.readable_tenant_ids if auth.is_cross_tenant_read and not tenant_id_explicit else None
        ),
    }
    rows = await get_storage_client().list_memories_by_filters(list_payload)

    has_more = len(rows) > limit
    items = [_memory_to_out(m) for m in rows[:limit]]

    next_cursor = None
    if has_more and items:
        last = rows[limit - 1]
        next_cursor = encode_cursor(datetime.fromisoformat(last["created_at"]), UUID(last["id"]))

    # Cross-tenant audit (F2): count per-tenant from served rows.
    source_tenants = auth.source_tenants_for_audit()
    if source_tenants and auth.is_cross_tenant_read and not tenant_id_explicit:
        counts: dict[str, int] = {}
        for row in rows[:limit]:
            rt = row.get("tenant_id")
            if rt:
                counts[rt] = counts.get(rt, 0) + 1
        await log_cross_tenant_read(
            home_tenant_id=auth.tenant_id,
            home_agent_id=auth.agent_id,
            source_tenants=source_tenants,
            surface="rest_memories_list",
            result_count_by_tenant=counts,
        )

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
):
    tenant_id_explicit = bool(tenant_id)
    if tenant_id:
        auth.enforce_readable_tenant(tenant_id)
    elif not auth.is_admin:
        if not auth.tenant_id:
            raise HTTPException(status_code=400, detail="tenant_id is required")
        tenant_id = auth.tenant_id

    # Type/agent/status breakdown (GROUPING SETS) via core-storage-api. Aggregates
    # across the readable set when the caller has cross-tenant read AND didn't pin
    # tenant_id; pinning to a specific tenant returns just that tenant's stats.
    return await get_storage_client().memory_stats_breakdown(
        {
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "agent_id": agent_id,
            "memory_type": memory_type,
            "status": status,
            "include_deleted": include_deleted,
            "readable_tenant_ids": (
                auth.readable_tenant_ids if auth.is_cross_tenant_read and not tenant_id_explicit else None
            ),
        }
    )


@router.get("/memories/count")
async def memory_count(
    tenant_id: str | None = Query(default=None),
    fleet_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
):
    """Count active (non-deleted) memories for a tenant, optionally a fleet.

    A lightweight operational primitive — for type/agent/status breakdowns use
    ``GET /memories/stats``. Declared BEFORE ``/memories/{memory_id}`` so the
    literal ``count`` segment resolves here instead of being parsed as a UUID
    (which previously returned a confusing 422).
    """
    # Tenant resolution mirrors memory_stats, with one deliberate difference:
    # count is single-tenant (count_active has no cross-tenant aggregate the way
    # compute_memory_stats does), so a tenant_id must ALWAYS be resolvable. An
    # admin omitting tenant_id therefore gets 400 — count_active(tenant_id=None)
    # is meaningless to storage (the /count-active endpoint requires tenant_id) —
    # rather than aggregating the way memory_stats does for admins.
    if tenant_id:
        auth.enforce_readable_tenant(tenant_id)
    elif not auth.is_admin:
        if not auth.tenant_id:
            raise HTTPException(status_code=400, detail="tenant_id is required")
        tenant_id = auth.tenant_id
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id is required")
    count = await get_storage_client().count_active(tenant_id, fleet_id)
    return {"count": count}


@router.delete("/memories", status_code=204)
async def delete_all_memories(
    tenant_id: str = Query(...),
    fleet_id: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    memory_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
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
    # BFLA gate: a bulk/whole-tenant delete by an *agent* credential requires
    # admin-trust (>= 3), matching single-delete (enforce_delete) and the trust
    # ladder — a routine trust-1 write key must not be able to wipe a fleet or
    # the tenant. Tenant/user credentials (no gateway X-Agent-ID → the tenant
    # owner) keep full reach (dashboard reset, tagged cleanup) unchanged.
    if auth.tenant_id and auth.agent_id:
        await enforce_delete(tenant_id, auth.agent_id)
    exclude_ids = (body or {}).get("exclude_ids", [])
    metadata_filter = (body or {}).get("metadata_filter") or {}
    if metadata_filter:
        # Validation stays in core-api so the exact 400 messages are preserved;
        # the storage endpoint builds the JSONB predicate via SQLAlchemy bound
        # params from the validated dict.
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
    deleted_count = await get_storage_client().soft_delete_by_filter(
        {
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "agent_id": agent_id,
            "memory_type": memory_type,
            "status": status,
            "exclude_ids": exclude_ids,
            "metadata_filter": metadata_filter or None,
        }
    )
    # Audit stays a decoupled async POST (not folded into the storage txn).
    await log_action(
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


@router.post("/memories/bulk-delete")
async def bulk_delete_by_ids(
    body: dict = Body(...),
    auth: AuthContext = Depends(get_auth_context),
):
    """Soft-delete memories by a list of IDs."""
    tenant_id = body.get("tenant_id")
    ids = body.get("ids", [])
    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id required")
    auth.enforce_read_only()
    auth.enforce_tenant(tenant_id)
    # BFLA gate (parity with delete_all_memories / single-delete): an agent
    # credential must be admin-trust (>= 3) to bulk-delete by id; this also
    # closes the cross-fleet/agent delete (the ids are otherwise unscoped).
    if auth.tenant_id and auth.agent_id:
        await enforce_delete(tenant_id, auth.agent_id)
    if not ids or len(ids) > 1000:
        raise HTTPException(status_code=400, detail="ids must be 1-1000 items")

    deleted = await get_storage_client().soft_delete_by_ids(tenant_id, ids)
    # Audit stays a decoupled async POST (not folded into the storage txn).
    await log_action(
        tenant_id=tenant_id,
        action="bulk_delete",
        resource_type="memory",
        detail={"count": deleted, "method": "by_ids"},
    )
    return {"deleted": deleted}


@router.get("/memories/{memory_id}")
async def get_memory(
    memory_id: UUID,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
):
    """Get a single memory with full details including embedding and entity links."""
    from fastapi.responses import JSONResponse

    auth.enforce_readable_tenant(tenant_id)

    t_start = time.perf_counter()
    hit = False
    error = False
    try:
        # Storage bundles the row + entity-link outerjoin + server-computed
        # embedding stats (raw pgvector never crosses the wire) in one call.
        detail = await get_storage_client().get_memory_detail(tenant_id, str(memory_id))
        if detail is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        memory = detail["memory"]

        # Fleet/agent-scope authorization: a by-id read must honor the same
        # scope_agent + cross-fleet trust ladder the list/search paths enforce,
        # so a same-tenant agent credential can't read a peer's scoped row by id.
        # 404 (not 403) mirrors ``enforce_memory_read`` — out-of-scope rows
        # simply don't exist for the caller. ``authorize_memory_access`` no
        # longer issues a DB query (its agent lookup routes through the storage
        # client), so ``db`` is threaded through but unused here; the ``get_db``
        # dependency stays only until the authz-helper ``db`` params are dropped
        # in the dedicated cleanup phase (then this read opens no connection).
        allowed = await authorize_memory_access(
            tenant_id,
            auth.agent_id,
            visibility=memory.get("visibility"),
            owner_agent_id=memory.get("agent_id"),
            fleet_id=memory.get("fleet_id"),
        )
        if not allowed:
            raise HTTPException(status_code=404, detail="Memory not found")

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
            "id": memory["id"],
            "tenant_id": memory["tenant_id"],
            "fleet_id": memory["fleet_id"],
            "agent_id": memory["agent_id"],
            "agent_display_name": memory.get("agent_display_name"),
            "memory_type": memory["memory_type"],
            "title": memory["title"],
            "content": memory["content"],
            "weight": float(memory["weight"]) if memory["weight"] is not None else None,
            "source_uri": memory["source_uri"],
            "run_id": memory["run_id"],
            # Storage serialises the JSONB column under ``metadata_``; the API
            # response exposes it as ``metadata``.
            "metadata": memory.get("metadata_"),
            "content_hash": memory["content_hash"],
            "created_at": memory["created_at"],
            "expires_at": memory["expires_at"],
            "deleted_at": memory["deleted_at"],
            "subject_entity_id": memory["subject_entity_id"],
            "predicate": memory["predicate"],
            "object_value": memory["object_value"],
            "ts_valid_start": memory["ts_valid_start"],
            "ts_valid_end": memory["ts_valid_end"],
            "status": memory["status"],
            "visibility": memory["visibility"],
            "recall_count": memory["recall_count"],
            "last_recalled_at": memory["last_recalled_at"],
            "supersedes_id": memory["supersedes_id"],
            "entity_links": detail["entity_links"],
            "embedding_preview": detail["embedding_preview"],
            "embedding_stats": detail["embedding_stats"],
        }
    )


@router.get("/memories/{memory_id}/contradictions")
async def get_contradictions(
    memory_id: UUID,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
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

    # Storage bundles the 3 reads (target row, supersessors, older) in one
    # round-trip and applies the cross-tenant ``older`` guard server-side.
    bundle = await get_storage_client().get_memory_contradictions(tenant_id, str(memory_id))
    if bundle is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    memory = bundle["memory"]

    # Fleet/agent-scope authorization (mirrors GET /memories/{id}): the
    # supersession chain below would otherwise leak a scoped row's contradiction
    # metadata to a same-tenant caller outside its fleet/agent scope.
    # ``authorize_memory_access`` issues no DB query (its agent lookup routes
    # through the storage client), so ``db`` is threaded through but unused; the
    # ``get_db`` dependency stays only until the authz-helper ``db`` params are
    # dropped in the dedicated cleanup phase.
    allowed = await authorize_memory_access(
        tenant_id,
        auth.agent_id,
        visibility=memory.get("visibility"),
        owner_agent_id=memory.get("agent_id"),
        fleet_id=memory.get("fleet_id"),
    )
    if not allowed:
        raise HTTPException(status_code=404, detail="Memory not found")

    supersessors = bundle["supersessors"]

    # The older row this memory replaced (if any). Despite the field
    # name, ``memory.supersedes_id`` points at the OLDER memory (the
    # detector's loser); this memory was the winner. The variable name
    # ``superseded_by`` in the response is preserved as-is for
    # back-compat; ``contradictions`` below uses unambiguous direction
    # labels. The cross-tenant guard already ran server-side; here we keep
    # the soft-deleted ``older`` filter (storage returns it regardless of
    # ``deleted_at`` so we can decide per-field).
    older = bundle["older"]
    older_live = older is not None and older.get("deleted_at") is None
    superseded_by = None
    if older_live:
        superseded_by = {
            "id": older["id"],
            "content_preview": older["content"][:200],
            "status": older["status"],
            "created_at": older["created_at"],
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
        primary = _reason_for(memory.get("status"))
        reason = primary if primary != "unknown" else _reason_for(m.get("status"))
        contradictions.append(
            {
                "memory_id": m["id"],
                "status": m["status"],
                "reason": reason,
                "content_preview": m["content"][:200],
                "direction": "superseded_by",
                "created_at": m["created_at"],
            }
        )
    # Direction "supersedes": the older row THIS memory replaced.
    if older_live:
        contradictions.append(
            {
                "memory_id": older["id"],
                "status": older["status"],
                "reason": _reason_for(older["status"]),
                "content_preview": older["content"][:200],
                "direction": "supersedes",
                "created_at": older["created_at"],
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
        "status": memory.get("status"),
        "superseded_by": superseded_by,
        "superseded_memories": [
            {
                "id": m["id"],
                "content_preview": m["content"][:200],
                "status": m["status"],
                "created_at": m["created_at"],
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
    idempotency_key: str | None = Header(None, alias=IDEMPOTENCY_HEADER),
):
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    auth.enforce_tenant(body.tenant_id)
    _reject_reserved_memory_type(body.memory_type)
    # Resolve a missing agent_id. On the standalone single-tenant path there is
    # one stable identity, so default to the reserved DEFAULT_AGENT_ID (mirrors
    # the evolve/insights REST routes) — this is what makes the documented
    # quickstart curl work without an agent_id. Everywhere else (tenant-scoped
    # key / enterprise gateway) the caller MUST name a real agent, or every
    # anonymous write would collapse onto one shared identity — the same footgun
    # mcp_server._refuse_default_agent_on_gateway guards against. Keep that as an
    # explicit 422 rather than a silent default.
    if not body.agent_id:
        if app_settings.is_standalone:
            body = body.model_copy(update={"agent_id": DEFAULT_AGENT_ID})
        else:
            raise _missing_agent_id_error()
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
        return await _write_memory_inner(body, response, auth, _idem)


async def _write_memory_inner(
    body: MemoryCreate,
    response: Response,
    auth: AuthContext,
    idem: IdempotencyGuard | None,
):
    from core_api.services.organization_settings import resolve_config

    write_config = await resolve_config(body.tenant_id)
    # Phase 2 (dark, default off): bind to the verified credential identity,
    # ignoring a client-supplied body override. Enable ONLY after reserved-
    # `main` creds are re-identified, else it pins them back onto `main`.
    if app_settings.bind_write_identity_to_auth and auth.agent_id:
        body.agent_id = auth.agent_id
    # Ownership boundary (gate + owner stamp + post-create re-check), shared
    # with the bulk path so a broker single-write can't attribute a memory to
    # an agent owned by a different install.
    agent, body.agent_id = await resolve_write_agent(
        body.agent_id,
        body.tenant_id,
        body.fleet_id,
        is_install_credential=auth.is_install_credential,
        install_uuid=auth.install_uuid,
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
        await enforce_fleet_write(body.tenant_id, body.agent_id, body.fleet_id)
        usage = await check_and_increment(body.tenant_id, "write")
    if usage:
        response.headers["X-RateLimit-Limit"] = str(usage.get("limit", "unlimited"))
        response.headers["X-RateLimit-Remaining"] = str(usage.get("remaining", "unlimited"))
    result = await create_memory(body)
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
    for _idx, _item in enumerate(body.items):
        _reject_reserved_memory_type(_item.memory_type, index=_idx)

    # Broker (kind=install_credential, ``mci_v1_…`` wire prefix) calls
    # don't carry an attempt id header or an ``agent_id`` body field
    # — the broker's own per-
    # session ``client_hash`` de-dup is the design contract per
    # cloud-data-plane.md §2.4 (gap G3). Server-derive a per-request
    # attempt id and attribute the write to the agent the batch's per-item
    # metadata names (memclawd Layer 1 stamps ``metadata.agent_id``); a mixed
    # or pre-Layer-1 batch falls back to the install. Non-broker
    # callers (dashboard, SDK) keep the CAURA-602 invariants in full.
    if auth.is_install_credential:
        if not bulk_attempt_id:
            import uuid as _uuid

            bulk_attempt_id = f"broker-{auth.install_uuid or 'unknown'}-{_uuid.uuid4()}"
        if not body.agent_id:
            body.agent_id = _broker_write_agent_id(body.items, auth.install_uuid)
        # The ownership gate + owner stamp + post-create re-check run in
        # ``resolve_write_agent`` inside ``_write_memories_bulk_inner`` (shared
        # with the single-write path). This block only does the bulk-specific
        # fan-in (derive the batch's agent from item metadata); the safety
        # gate applies to that result — and to an explicitly-supplied
        # ``agent_id`` — there.
    # Resolve a missing agent_id (mirrors write_memory). Install-credential
    # callers were already attributed above; everyone else either gets the
    # reserved standalone identity or must name a real agent. Defaulting
    # outside standalone would silently collapse anonymous writes onto one
    # shared identity — see mcp_server._refuse_default_agent_on_gateway.
    if not body.agent_id:
        if app_settings.is_standalone:
            body = body.model_copy(update={"agent_id": DEFAULT_AGENT_ID})
        else:
            raise _missing_agent_id_error()
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
        return await _write_memories_bulk_inner(body, response, auth, _idem, bulk_attempt_id)


def _broker_write_agent_id(items: list[BulkMemoryItem], install_uuid: str | None) -> str:
    """Attribute a broker (install-credential) bulk write to the agent that
    produced it, when the batch unambiguously names one.

    Each item's ``metadata.agent_id`` is stamped by the broker from the
    capturing agent (memclawd Layer 1). When every item that carries an
    agent_id agrees on a single value, the write is attributed to that agent —
    so the memory view names the agent, not the bare install. Items with no
    agent_id abstain rather than veto, so a mixed pre-Layer-1/Layer-1 batch that
    still names a single agent attributes to it. A batch where named items
    disagree, or where no item carries an agent_id at all, falls back to the
    install identity; a write is never mis-attributed to the wrong agent.
    """
    agent_ids: set[str] = set()
    for item in items:
        aid = (item.metadata or {}).get("agent_id")
        if isinstance(aid, str) and aid:
            agent_ids.add(aid)
    if len(agent_ids) == 1:
        return next(iter(agent_ids))
    return broker_label(install_uuid)


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
    idem: IdempotencyGuard | None,
    bulk_attempt_id: str,
):
    # Phase 2 (dark, default off): bind to the verified credential identity
    # (see _write_memory_inner). Enabled only post-re-identification. Runs
    # before resolve_write_agent so the gate/stamp apply to the bound identity.
    if app_settings.bind_write_identity_to_auth and auth.agent_id:
        body.agent_id = auth.agent_id
    # Ownership boundary (gate + owner stamp + post-create re-check), shared
    # with the single-write path.
    #
    # NOTE: unlike single-write (_write_memory_inner) and the MCP write tool,
    # bulk deliberately does NOT enforce the per-agent approval gate
    # (require_agent_approval / trust_level==0): it passes no require_approval and
    # has no trust==0 check. Bulk is the broker (memclawd) fan-in path that
    # auto-registers many agents from item metadata; gating each on admin
    # approval would create trust-0 rows and 403 whole batches, breaking capture.
    # Per-agent approval is an interactive / single-agent concern.
    agent, body.agent_id = await resolve_write_agent(
        body.agent_id,
        body.tenant_id,
        body.fleet_id,
        is_install_credential=auth.is_install_credential,
        install_uuid=auth.install_uuid,
    )
    if not body.fleet_id and agent.get("fleet_id"):
        body.fleet_id = agent["fleet_id"]
    usage = None
    if auth.tenant_id:  # skip enforcement + metering for admin
        await enforce_fleet_write(body.tenant_id, body.agent_id, body.fleet_id)
        usage = await bulk_check_and_increment(body.tenant_id, len(body.items))
    if usage:
        response.headers["X-RateLimit-Limit"] = str(usage.get("limit", "unlimited"))
        response.headers["X-RateLimit-Remaining"] = str(usage.get("remaining", "unlimited"))

    try:
        result = await asyncio.wait_for(
            create_memories_bulk(body, bulk_attempt_id=bulk_attempt_id),
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
):
    auth.enforce_read_only()
    auth.enforce_tenant(tenant_id)
    # Authenticated agent identity (gateway X-Agent-ID) takes precedence over
    # the caller-supplied query param so an agent credential can't skip the
    # trust gate by omitting/spoofing ``agent_id``.
    caller_agent_id = auth.agent_id or agent_id
    if auth.tenant_id and caller_agent_id:
        await enforce_delete(tenant_id, caller_agent_id)
        # Cross-fleet / scope_agent row authorization (write threshold).
        # ``get_memory_for_tenant`` already filters out soft-deleted /
        # cross-tenant rows server-side, so a returned row is live + owned.
        target = await get_storage_client().get_memory_for_tenant(tenant_id, str(memory_id))
        if target:
            allowed = await authorize_memory_access(
                tenant_id,
                caller_agent_id,
                visibility=target.get("visibility"),
                owner_agent_id=target.get("agent_id"),
                fleet_id=target.get("fleet_id"),
                write=True,
            )
            if not allowed:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Agent '{caller_agent_id}' cannot delete memory in fleet '{target.get('fleet_id')}'."
                    ),
                )
    # ``soft_delete_memory`` already routes the fetch + delete through the
    # storage client (and logs its own ``soft_delete`` audit row).
    await soft_delete_memory(memory_id, tenant_id)
    await log_action(
        tenant_id=tenant_id,
        # Attribute to the effective identity (gateway X-Agent-ID wins over
        # the query param) — logging the raw param attributed a gateway
        # agent's deletes to None whenever it omitted ``agent_id``.
        agent_id=caller_agent_id,
        action="delete",
        resource_type="memory",
        resource_id=memory_id,
    )


@router.patch("/memories/{memory_id}/status")
async def update_memory_status(
    memory_id: UUID,
    body: dict,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
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
    sc = get_storage_client()
    # ``get_memory_for_tenant`` filters out soft-deleted / cross-tenant rows
    # server-side, so a returned row is live + owned.
    memory = await sc.get_memory_for_tenant(tenant_id, str(memory_id))
    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")
    # Cross-fleet / scope_agent row authorization for the authenticated agent
    # (no-op for tenant-scoped dashboard credentials, where auth.agent_id is None).
    if auth.agent_id:
        allowed = await authorize_memory_access(
            tenant_id,
            auth.agent_id,
            visibility=memory.get("visibility"),
            owner_agent_id=memory.get("agent_id"),
            fleet_id=memory.get("fleet_id"),
            write=True,
        )
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Agent '{auth.agent_id}' cannot modify memory in fleet '{memory.get('fleet_id')}'.",
            )
    old_status = memory.get("status")
    await sc.update_memory_status(str(memory_id), status, tenant_id=tenant_id)

    # Audit stays a decoupled async POST (not folded into the storage txn).
    await log_action(
        tenant_id=tenant_id,
        agent_id=memory.get("agent_id"),
        action="status_update",
        resource_type="memory",
        resource_id=memory_id,
        detail={"old_status": old_status, "new_status": status},
    )
    return {"memory_id": str(memory_id), "old_status": old_status, "new_status": status}


@router.patch("/memories/{memory_id}", response_model=MemoryOut)
async def update_memory_endpoint(
    memory_id: UUID,
    body: MemoryUpdate,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(default=None),
    metadata_mode: str | None = Query(
        default=None,
        pattern="^(merge|replace)$",
        description=(
            "C7: alias for the body field of the same name. Body wins on "
            "conflict. When supplied as a query param without a matching "
            "``metadata`` field in the body, returns 422 (same contract "
            "as the body-side validator). Provided so SDK callers that "
            "thread the toggle through a URL query don't silently get "
            "the default merge behaviour."
        ),
    ),
    auth: AuthContext = Depends(get_auth_context),
):
    """Update a memory. Only provide fields you want to change.

    If content changes, embedding and entity extraction are re-run automatically.

    Concurrency contract (intentional, not a bug):

    - Each top-level column is **last-write-wins**. Two concurrent PATCHes that
      set the same column (e.g. ``title``) will both return 200 and the row
      will hold whichever update commits second. There is no compare-and-swap
      and no optimistic-lock token — callers that need that must coordinate
      externally or use ``If-Match`` semantics layered on top.
    - ``metadata`` defaults to a **JSONB top-level merge** (``||``): keys not
      present in the patch are preserved, keys present in both patches resolve
      last-write-wins per key. Pass ``metadata_mode: "replace"`` for a full
      overwrite. Two concurrent PATCHes that each set a distinct metadata key
      will therefore leave **both keys present** on the row — that is the
      defined behaviour, not a partial-merge anomaly.
    """
    auth.enforce_tenant(tenant_id)
    # C7: propagate ``?metadata_mode=...`` query param into the body when
    # body didn't supply its own. Body wins on conflict, so a caller who
    # sends both ?metadata_mode=replace AND {"metadata_mode": "merge"} in
    # the body gets merge — the body field is the canonical write
    # payload, the query is a convenience shim. Mirror the body-side
    # validator (``metadata_mode_requires_metadata``) by rejecting the
    # query param when body has no ``metadata`` patch — sending only the
    # mode flag is a real-value no-op the validator already rejects.
    if metadata_mode is not None and body.metadata_mode is None:
        if "metadata" not in body.model_fields_set:
            raise HTTPException(
                status_code=422,
                detail="metadata_mode is only valid when metadata is also provided",
            )
        body.metadata_mode = metadata_mode
    if auth.tenant_id:  # skip usage metering for admin
        await check_and_increment(tenant_id, "write")
    # Authenticated agent identity (gateway X-Agent-ID) takes precedence over
    # the caller-supplied query param for trust/fleet enforcement.
    return await update_memory(
        memory_id,
        tenant_id,
        body,
        agent_id=(auth.agent_id or agent_id) if auth.tenant_id else None,
    )


@router.post("/search", response_model=SearchResponse)
@search_limit
async def search(
    request: Request,
    body: SearchRequest,
    response: Response,
    auth: AuthContext = Depends(get_auth_context),
):
    # Read endpoint — honors cross-tenant readable set. The SQL
    # widens to readable_tenant_ids inside search_memories so the
    # body.tenant_id can be any tenant the caller may read from
    # (typically home, but explicit source queries work too).
    auth.enforce_readable_tenant(body.tenant_id)
    async with per_tenant_slot("search", body.tenant_id):
        return await _search_inner(body, response, auth)


async def _search_inner(
    body: SearchRequest,
    response: Response,
    auth: AuthContext,
):
    usage = None
    _agent = None
    # Effective caller identity for scope/fleet enforcement: an explicit
    # filter_agent_id, else the gateway-authenticated agent (auth.agent_id).
    # Falling back to auth.agent_id closes the search side of the BOLA chain —
    # a constrained agent that OMITS filter_agent_id must still be fleet- and
    # scope_agent-bound, not handed tenant-wide content (search returns full
    # content, so this is a direct disclosure, not just id harvesting). A
    # tenant/user credential (auth.agent_id None, no filter) keeps full-tenant
    # search, unchanged.
    eff_agent_id = body.filter_agent_id or auth.agent_id
    if auth.tenant_id:  # skip for admin
        if eff_agent_id:
            fleet_id_hint = body.fleet_ids[0] if body.fleet_ids and len(body.fleet_ids) == 1 else None
            _agent = await get_or_create_agent(body.tenant_id, eff_agent_id, fleet_id_hint)
            if not body.fleet_ids and _agent.get("fleet_id") and _agent.get("trust_level", 0) < 2:
                body.fleet_ids = [_agent["fleet_id"]]  # Force fleet scoping for trust < 2
            if body.fleet_ids and len(body.fleet_ids) == 1:
                await enforce_fleet_read(body.tenant_id, eff_agent_id, body.fleet_ids[0])
        usage = await check_and_increment(body.tenant_id, "search")
    if usage:
        response.headers["X-RateLimit-Limit"] = str(usage.get("limit", "unlimited"))
        response.headers["X-RateLimit-Remaining"] = str(usage.get("remaining", "unlimited"))
    from core_api.services.organization_settings import resolve_config

    t_start = time.perf_counter()
    success = True
    results: list = []
    try:
        config = await resolve_config(body.tenant_id)
        # Widen the read predicate when the caller authenticated with
        # a cross-tenant key. Single-tenant keys leave
        # ``readable_tenant_ids = [tenant_id]`` so this is a no-op.
        results = await search_memories(
            tenant_id=body.tenant_id,
            query=body.query,
            fleet_ids=body.fleet_ids,
            filter_agent_id=body.filter_agent_id,
            # Visibility identity is the authenticated agent (not just an
            # explicit filter) so the caller sees its own scope_agent rows and
            # nobody else's, even when filter_agent_id is omitted.
            caller_agent_id=eff_agent_id,
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
):
    """Extract facts from a URL or text for preview (no writes).

    Body size cap: enforced upstream by ``IngestBodySizeMiddleware`` (PR #9).
    """
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    auth.enforce_tenant(body.tenant_id)
    return await ingest_preview(body)


@router.post("/ingest/commit")
@write_limit
async def ingest_commit_endpoint(
    request: Request,
    body: IngestCommitRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """Write previewed facts as memories."""
    auth.enforce_read_only()
    auth.enforce_usage_limits()
    auth.enforce_tenant(body.tenant_id)
    # Broker ownership boundary: degrade a foreign / reserved agent id to the
    # install's own broker:<install> fallback so a broker can't attribute an
    # ingested memory to an agent owned by another install (parity with the
    # data-plane write paths; ingest_commit itself takes no AuthContext).
    if auth.is_install_credential and body.agent_id:
        body.agent_id = await broker_owned_agent_id(body.agent_id, auth.install_uuid, body.tenant_id)
    if auth.tenant_id:  # skip for admin
        await check_and_increment(body.tenant_id, "write")
    return await ingest_commit(body)


@router.post("/ingest/file")
async def ingest_file_endpoint(
    file: UploadFile = File(...),
    tenant_id: str = Form(...),
    focus: str | None = Form(None),
    fleet_id: str | None = Form(None),
    agent_id: str | None = Form(None),
    auth: AuthContext = Depends(get_auth_context),
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
    return await ingest_preview(req)


@router.post("/ingest/undo/{run_id}")
async def ingest_undo_endpoint(
    run_id: str,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
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

    sc = get_storage_client()
    # Soft-delete the memory rows server-side (filters by run_id AND
    # ``metadata.source = "ingest"`` AND tenant ownership, same predicate as
    # the prior inline UPDATE).
    deleted_count = await sc.soft_delete_by_run(tenant_id, run_id, metadata_source="ingest")

    # Delete the parent Document for this batch (introduced by the
    # parent-doc PR). Best-effort: older batches predate the parent-doc
    # write and won't have one, which is fine — undo on those still
    # soft-deletes the memories, just without a parent record to drop.
    # Kept as a separate storage call in the same order as before.
    try:
        await sc.delete_document(tenant_id=tenant_id, collection=INGEST_DOCUMENTS_COLLECTION, doc_id=run_id)
    except Exception:
        logger.info(
            "ingest_undo: parent Document delete failed or no-op (run_id=%s) — "
            "memories soft-deleted regardless",
            run_id,
            exc_info=False,
        )

    # Audit stays a decoupled async POST (not folded into the storage txn).
    await log_action(
        tenant_id=tenant_id,
        action="ingest_undo",
        resource_type="memory",
        detail={"run_id": run_id, "count": deleted_count},
    )
    return {"deleted": deleted_count, "run_id": run_id}


@router.post("/recall")
@search_limit
async def recall_endpoint(
    request: Request,
    body: SearchRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """Search memories and return an LLM-synthesized context summary.

    Audit P3 (extended to REST): the legacy ``recall(...)`` wrapper
    held the FastAPI-injected DB session across the multi-second LLM
    brief, pinning a pooled connection. Load-test gate flagged
    ``slo-p95-recall_brief`` (6.9 s vs 5 s target) and
    ``noisy-neighbor-write`` (6.49x regression under search storm) —
    both rooted in the same pool-pinning pattern.

    Mitigation: do the DB-bound search inside the request session,
    explicitly ``await db.close()`` to return the connection to the
    pool, then call ``summarize_memories`` (the no-DB LLM helper from
    PR #228). ``AsyncSession.close()`` is idempotent, so FastAPI's
    dependency cleanup at request end runs harmlessly.
    """
    # Read endpoint — readable set widening applies (see /search).
    auth.enforce_readable_tenant(body.tenant_id)
    if auth.tenant_id:
        if body.filter_agent_id:
            fleet_id_hint = body.fleet_ids[0] if body.fleet_ids and len(body.fleet_ids) == 1 else None
            _agent = await get_or_create_agent(body.tenant_id, body.filter_agent_id, fleet_id_hint)
            if not body.fleet_ids and _agent.get("fleet_id") and _agent.get("trust_level", 0) < 2:
                body.fleet_ids = [_agent["fleet_id"]]
            if body.fleet_ids and len(body.fleet_ids) == 1:
                await enforce_fleet_read(body.tenant_id, body.filter_agent_id, body.fleet_ids[0])
        await check_and_increment(body.tenant_id, "search")

    from core_api.services.memory_service import search_memories
    from core_api.services.organization_settings import resolve_config
    from core_api.services.recall_service import summarize_memories

    t0 = time.perf_counter()

    # ── Phase 1: DB-bound — config + search ──────────────────────
    config = await resolve_config(body.tenant_id)
    memories = await search_memories(
        tenant_id=body.tenant_id,
        query=body.query,
        fleet_ids=body.fleet_ids,
        filter_agent_id=body.filter_agent_id,
        caller_agent_id=body.filter_agent_id,
        memory_type_filter=body.memory_type_filter,
        status_filter=body.status_filter,
        top_k=body.top_k,
        valid_at=body.valid_at,
        recall_boost=config.recall_boost,
        graph_expand=config.graph_expand,
        tenant_config=config,
        readable_tenant_ids=auth.readable_tenant_ids if auth.is_cross_tenant_read else None,
    )

    # Release the pooled DB connection before the LLM round-trip.
    # FastAPI's outer ``get_db`` ``async with`` will re-close on exit —
    # idempotent, so it's a no-op there.

    # ── Phase 2: LLM brief (no DB held) ──────────────────────────
    return await summarize_memories(
        memories,
        body.query,
        config,
        valid_at=body.valid_at,
        top_k=body.top_k,
        t0=t0,
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

    # Authenticated agent identity (gateway X-Agent-ID) takes precedence over
    # the caller-supplied query param: running the trust gate against a
    # caller-asserted ``agent_id`` would let a low-trust agent credential
    # clear it by naming some trust-3 agent in the query string (privilege
    # escalation). Mirrors the precedence pattern in delete/update_memory.
    if auth.agent_id and agent_id != auth.agent_id:
        raise HTTPException(
            status_code=403,
            detail=(
                f"agent_id '{agent_id}' does not match the authenticated agent identity '{auth.agent_id}'."
            ),
        )

    # Verify requesting agent is admin
    caller = await lookup_agent(tenant_id, agent_id)
    if caller is None or caller.get("trust_level", 0) < 3:
        raise HTTPException(
            status_code=403,
            detail=f"Agent '{agent_id}' requires trust_level >= 3 for redistribute.",
        )

    # Verify target agent exists and is not restricted
    target = await lookup_agent(tenant_id, body.target_agent_id)
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
        await bulk_check_and_increment(tenant_id, len(body.memory_ids))

    # The SELECT … FOR UPDATE, the moved/promoted/skipped/from_agents loop,
    # the scope_agent→scope_team auto-promotion, and the not_found computation
    # all run server-side in ONE storage transaction (NOT N per-row HTTP
    # calls). The trust gates + agent_id==auth precedence above stay in
    # core-api, BEFORE the call.
    outcome = await get_storage_client().redistribute_memories(
        tenant_id,
        [str(mid) for mid in body.memory_ids],
        body.target_agent_id,
    )

    # Audit stays a decoupled async POST (not folded into the storage txn).
    await log_action(
        tenant_id=tenant_id,
        agent_id=agent_id,
        action="redistribute",
        resource_type="memory",
        detail={
            "target_agent_id": body.target_agent_id,
            "from_agents": outcome["from_agents"],
            "moved": outcome["moved"],
            "promoted": outcome["promoted"],
            "skipped": outcome["skipped"],
            "requested": len(body.memory_ids),
        },
    )

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return RedistributeResponse(
        moved=outcome["moved"],
        promoted=outcome["promoted"],
        skipped=outcome["skipped"],
        errors=outcome["not_found"],
        redistribute_ms=elapsed_ms,
    )


# ---------------------------------------------------------------------------
# Admin global-view endpoints (requires enforce_admin)
# ---------------------------------------------------------------------------

admin_memories_router = APIRouter(tags=["Admin"])


@admin_memories_router.get("/admin/tenants")
async def admin_list_tenants(
    auth: AuthContext = Depends(get_auth_context),
):
    """Admin: list all tenant IDs that have memories."""
    auth.enforce_admin()
    return await list_active_tenant_ids()


@admin_memories_router.get("/admin/fleets")
async def admin_list_fleets(
    tenant_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
):
    """Admin: list distinct fleet_ids with memory counts."""
    auth.enforce_admin()
    # Admin view: no ``scope_agent`` exclusion (admins see everything), and
    # cross-tenant when ``tenant_id`` is omitted.
    return await get_storage_client().memory_fleet_distribution(tenant_id, exclude_scope_agent=False)


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
):
    """Admin: list memories across all tenants with full pagination."""
    auth.enforce_admin()

    if cursor and (sort != "created_at" or order != "desc"):
        raise HTTPException(
            status_code=400,
            detail="Cursor pagination is only supported with sort=created_at and order=desc",
        )
    cursor_ts = cursor_id = None
    if cursor:
        try:
            cursor_ts, cursor_id = decode_cursor(cursor)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid cursor")

    # Request ``limit + 1`` rows from storage so we can detect ``has_more``
    # and build the next cursor here; storage applies the same filter,
    # cursor predicate, and ``(sort, id)`` tiebreaker the prior inline query
    # used. NO visibility scoping (admin view).
    payload: dict = {
        "include_deleted": include_deleted,
        "sort": sort,
        "order": order,
        "offset": offset,
        "limit": limit + 1,
    }
    if tenant_id:
        payload["tenant_id"] = tenant_id
    if fleet_id:
        payload["fleet_id"] = fleet_id
    if agent_id:
        payload["agent_id"] = agent_id
    if memory_type:
        payload["memory_type"] = memory_type
    if status:
        payload["status"] = status
    if cursor_ts is not None and cursor_id is not None:
        payload["cursor_ts"] = cursor_ts.isoformat()
        payload["cursor_id"] = str(cursor_id)

    rows = await get_storage_client().admin_list_memories(payload)

    has_more = len(rows) > limit
    page = rows[:limit]
    # ``_memory_to_out`` accepts either an ORM row or a storage dict.
    items = [_memory_to_out(r) for r in page]

    next_cursor = None
    if has_more and page:
        last = page[limit - 1]
        next_cursor = encode_cursor(
            datetime.fromisoformat(last["created_at"]),
            UUID(last["id"]),
        )

    return PaginatedMemoryResponse(items=items, next_cursor=next_cursor)


@admin_memories_router.get("/admin/memories/stats")
async def admin_memory_stats(
    tenant_id: str | None = Query(default=None),
    fleet_id: str | None = Query(default=None),
    auth: AuthContext = Depends(get_auth_context),
):
    """Admin: memory stats across all tenants (or filtered by tenant_id)."""
    auth.enforce_admin()
    # Single GROUPING SETS scan server-side (storage-api), shaped as
    # {total, by_type, by_agent, by_status}. The prior inline 4-query
    # implementation + transient-DB fallback are gone — storage-api owns the
    # DB and the storage client carries its own retry budget for transient
    # blips.
    return await get_storage_client().admin_memory_stats(tenant_id, fleet_id)
