"""MCP (Model Context Protocol) server for MemClaw.

Exposes MemClaw tools over Streamable HTTP so any MCP client
(Claude Desktop, Claude Code, Cursor, etc.) can connect with just a URL + API key.

Mounted onto the main FastAPI app at /mcp.
"""

import contextlib
import contextvars
import hmac as _hmac
import json
import logging
import time
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, ValidationError
from sqlalchemy import text as sa_text
from starlette.types import ASGIApp, Receive, Scope, Send

from core_api.auth import get_admin_key
from core_api.constants import (
    DEFAULT_SEARCH_TOP_K,
    EVOLVE_OUTCOME_TYPES,
    INSIGHTS_FOCUS_MODES,
    MAX_SEARCH_TOP_K,
    MEMORY_STATUSES,
    MEMORY_TYPES,
    VALID_SCOPES,
    VERSION,
)
from core_api.db.session import async_session
from core_api.errors import code_for_status
from core_api.repositories import memory_repo
from core_api.schemas import BulkMemoryCreate, BulkMemoryItem, MemoryCreate, MemoryUpdate
from core_api.services.audit_service import log_action
from core_api.services.entity_service import get_entity
from core_api.services.memory_service import (
    create_memories_bulk,
    create_memory,
    search_memories,
    soft_delete_memory,
    update_memory,
)

# Re-export so existing `monkeypatch.setattr(mcp_server, "_require_trust", ...)`
# sites in tests keep working; production callers should import ``require_trust``
# directly from ``core_api.services.trust_service``.
from core_api.services.trust_service import parse_trust_error
from core_api.services.trust_service import require_trust as _require_trust
from core_api.services.usage_service import check_and_increment_by_tenant as check_and_increment

logger = logging.getLogger(__name__)

# ── Auth via context vars ──

_tenant_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_tenant_id")
_agent_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("mcp_agent_id", default=None)

_UNAUTH = "__unauthenticated__"
_ADMIN = "__admin__"
_NO_AUTH = "__no_auth__"


def _error_response(code: str, message: str, **details) -> str:
    """Return the canonical MCP error envelope as a JSON string.

    Shape matches the REST surface (see ``core_api.errors.make_error_payload``):
    ``{"error": {"code": "...", "message": "...", "details": {...}}}``.

    Wrap with ``_with_latency(...)`` when an in-tool latency stamp is desired —
    ``_with_latency`` parses the JSON, appends ``_latency_ms``, and re-serializes.
    """
    from core_api.errors import make_error_payload

    payload = make_error_payload(code, message, details=details if details else None)
    return json.dumps(payload, default=str)


# Pre-tool auth errors (returned directly, NOT through _with_latency, because
# they fire before any tool work has begun).
_AUTH_ERROR = _error_response(
    "UNAUTHORIZED", "Missing or invalid X-API-Key header. Provide a tenant-scoped API key."
)
_ADMIN_ERROR = _error_response(
    "FORBIDDEN", "Admin/system keys cannot be used with MCP. Use a tenant-scoped API key."
)


class MCPAuthMiddleware:
    """ASGI middleware that resolves X-API-Key to tenant_id before MCP handlers run."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            from core_api.config import settings

            headers = dict(scope.get("headers", []))

            # Preferred path: enterprise nginx has already validated the
            # session cookie / JWT / API key via auth_request and injected
            # X-Tenant-ID. Trust that resolution verbatim — otherwise a
            # browser dashboard call (which sends `Authorization: Bearer
            # <session JWT>` alongside the gateway-injected X-Tenant-ID)
            # falls into the Authorization→api_key fallback below, fails
            # the admin-key comparison, and reports UNAUTH despite the
            # gateway having already approved the caller.
            tenant_header = headers.get(b"x-tenant-id", b"").decode()
            if tenant_header:
                _tenant_id_var.set(tenant_header)
            else:
                api_key = headers.get(b"x-api-key", b"").decode()
                if not api_key:
                    auth_header = headers.get(b"authorization", b"").decode()
                    if auth_header.lower().startswith("bearer "):
                        api_key = auth_header[7:]
                admin_key = get_admin_key()

                if admin_key and api_key and _hmac.compare_digest(api_key, admin_key):
                    _tenant_id_var.set(_ADMIN)
                elif settings.is_standalone:
                    from core_api.standalone import get_standalone_tenant_id

                    _tenant_id_var.set(get_standalone_tenant_id())
                elif not api_key:
                    _tenant_id_var.set(_UNAUTH if admin_key else _NO_AUTH)
                else:
                    _tenant_id_var.set(_UNAUTH)

            # X-Agent-ID injected by enterprise gateway for mca_ agent keys.
            # When present, this is the cryptographically verified agent identity.
            agent_header = headers.get(b"x-agent-id", b"").decode()
            _agent_id_var.set(agent_header or None)

        await self.app(scope, receive, send)


def _get_tenant() -> str:
    return _tenant_id_var.get(_UNAUTH)


def _get_agent_id() -> str | None:
    """Return the verified agent_id from X-Agent-ID header, or None."""
    return _agent_id_var.get(None)


def _check_auth() -> str | None:
    """Return an error string if auth fails, None if OK."""
    tid = _get_tenant()
    if tid == _UNAUTH:
        return _AUTH_ERROR
    if tid in (_ADMIN, _NO_AUTH):
        return _ADMIN_ERROR
    return None


@contextlib.asynccontextmanager
async def _mcp_session():
    """Session with RLS tenant context set from MCP auth."""
    async with async_session() as session:
        tenant_id = _get_tenant()
        if tenant_id and tenant_id not in (_UNAUTH, _ADMIN, _NO_AUTH):
            await session.execute(
                sa_text("SELECT set_config('app.tenant_id', :tid, true)"),
                {"tid": tenant_id},
            )
        else:
            await session.execute(sa_text("SELECT set_config('app.tenant_id', '', true)"))
        yield session


# ── FastMCP instance ──

mcp = FastMCP(
    name=f"MemClaw v{VERSION}",
    instructions=(
        "MemClaw is a persistent memory platform for AI agents. "
        "Use these tools to write, search, delete, and manage memories and entities. "
        "Memories are auto-enriched with type, title, summary, and tags via LLM. "
        "Just provide the content — MemClaw handles the rest. "
        "First-time setup: install the 'memclaw' usage skill via this server's "
        "/api/v1/install-skill endpoint (see README § 'Install the skill'). The "
        "skill teaches agents when and how to use these 12 tools."
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _serialize(obj) -> str:
    if isinstance(obj, list):
        return json.dumps([item.model_dump(mode="json") for item in obj], indent=2, default=str)
    return json.dumps(obj.model_dump(mode="json"), indent=2, default=str)


def _with_latency(result: str, t0: float) -> str:
    """Append _latency_ms to response text."""
    ms = round((time.perf_counter() - t0) * 1000)
    try:
        data = json.loads(result)
        if isinstance(data, dict):
            data["_latency_ms"] = ms
            return json.dumps(data, default=str)
    except (json.JSONDecodeError, ValueError):
        pass
    return result + f"\n\n_latency_ms: {ms}"


# ── Tools ──


async def memclaw_recall(
    query: Annotated[str, Field(description="NL query.")],
    agent_id: Annotated[str, Field(description="Caller agent.")] = "mcp-agent",
    filter_agent_id: Annotated[str | None, Field(description="Filter by author.")] = None,
    memory_type: Annotated[str | None, Field(description="Filter by type.")] = None,
    status: Annotated[str | None, Field(description="Filter by status.")] = None,
    fleet_ids: Annotated[list[str] | None, Field(description="Restrict fleets.")] = None,
    include_brief: Annotated[bool, Field(description="Add LLM summary.")] = False,
    top_k: Annotated[int, Field(description="1-20.")] = DEFAULT_SEARCH_TOP_K,
) -> str:
    """Hybrid semantic+keyword recall, with optional LLM brief."""
    t0 = time.perf_counter()
    if err := _check_auth():
        return err
    if memory_type and memory_type not in MEMORY_TYPES:
        return _error_response(
            "INVALID_ARGUMENTS",
            f"Invalid memory_type '{memory_type}'. Must be one of: {', '.join(MEMORY_TYPES)}",
            field="memory_type",
            value=memory_type,
        )
    if status and status not in MEMORY_STATUSES:
        return _error_response(
            "INVALID_ARGUMENTS",
            f"Invalid status '{status}'. Must be one of: {', '.join(MEMORY_STATUSES)}",
            field="status",
            value=status,
        )
    tenant_id = _get_tenant()
    agent_id = _get_agent_id() or agent_id  # prefer gateway-verified identity
    capped_top_k = min(top_k, MAX_SEARCH_TOP_K)

    async with _mcp_session() as db:
        try:
            await check_and_increment(db, tenant_id, "search")
            from core_api.repositories import agent_repo
            from core_api.services.organization_settings import resolve_config

            config = await resolve_config(db, tenant_id)
            _ag = await agent_repo.get_by_id(db, agent_id, tenant_id)
            agent_profile = None
            if _ag:
                agent_profile = _ag.get("search_profile") if isinstance(_ag, dict) else _ag.search_profile
            results = await search_memories(
                db,
                tenant_id=tenant_id,
                query=query,
                fleet_ids=fleet_ids,
                filter_agent_id=filter_agent_id,
                caller_agent_id=agent_id,
                memory_type_filter=memory_type,
                status_filter=status,
                top_k=capped_top_k,
                recall_boost=config.recall_boost,
                graph_expand=config.graph_expand,
                tenant_config=config,
                search_profile=agent_profile,
            )
            payload: dict = {
                "results": [r.model_dump(mode="json") for r in results] if results else [],
            }
            if include_brief:
                from core_api.services.recall_service import recall as _recall

                brief = await _recall(
                    db,
                    tenant_id=tenant_id,
                    query=query,
                    fleet_ids=fleet_ids,
                    filter_agent_id=filter_agent_id,
                    caller_agent_id=agent_id,
                    memory_type_filter=memory_type,
                    status_filter=status,
                    top_k=capped_top_k,
                )
                payload["brief"] = brief
            return _with_latency(json.dumps(payload, indent=2, default=str), t0)
        except HTTPException as e:
            logger.warning("MCP tool error (%s): %s", e.status_code, e.detail)
            return _with_latency(_error_response(code_for_status(e.status_code), str(e.detail)), t0)


async def memclaw_write(
    content: Annotated[str | None, Field(description="Single-write text.")] = None,
    items: Annotated[
        list[dict] | None, Field(description="Batch of objects, ≤100; each needs 'content'.")
    ] = None,
    agent_id: Annotated[str, Field(description="Caller agent.")] = "mcp-agent",
    fleet_id: Annotated[str | None, Field(description="Fleet scope.")] = None,
    visibility: Annotated[str | None, Field(description="scope_team|scope_org|scope_agent.")] = None,
    memory_type: Annotated[str | None, Field(description="Type (single only).")] = None,
    weight: Annotated[float | None, Field(description="0-1 (single only).")] = None,
    source_uri: Annotated[str | None, Field(description="Source URI (single only).")] = None,
    run_id: Annotated[str | None, Field(description="Run id (single only).")] = None,
    metadata: Annotated[dict | None, Field(description="Metadata (single only).")] = None,
    status: Annotated[str | None, Field(description="Status (single only).")] = None,
    write_mode: Annotated[str | None, Field(description="fast|strong|auto (single only).")] = None,
) -> str:
    """Single OR batch write. Exactly one of {content, items} is required."""
    t0 = time.perf_counter()
    if err := _check_auth():
        return err
    if (content is None) == (items is None):
        return _with_latency(
            json.dumps(
                {
                    "error": {
                        "code": "INVALID_ARGUMENTS",
                        "message": "memclaw_write requires exactly one of {content, items}.",
                        "details": {
                            "received_content": content is not None,
                            "received_items": items is not None,
                            "resolution": "omit one",
                        },
                    }
                }
            ),
            t0,
        )
    tenant_id = _get_tenant()
    agent_id = _get_agent_id() or agent_id

    async with _mcp_session() as db:
        try:
            if content is not None:
                await check_and_increment(db, tenant_id, "write")
                result = await create_memory(
                    db,
                    MemoryCreate(
                        tenant_id=tenant_id,
                        fleet_id=fleet_id,
                        agent_id=agent_id,
                        memory_type=memory_type,
                        content=content,
                        weight=weight,
                        source_uri=source_uri,
                        run_id=run_id,
                        metadata=metadata,
                        status=status,
                        visibility=visibility,
                        write_mode=write_mode,
                    ),
                )
                return _with_latency(_serialize(result), t0)
            # Batch path
            if len(items) > 100:
                return _with_latency(
                    json.dumps(
                        {
                            "error": {
                                "code": "BATCH_TOO_LARGE",
                                "message": f"items length {len(items)} exceeds maximum of 100.",
                                "details": {"received": len(items), "max": 100},
                            }
                        }
                    ),
                    t0,
                )
            try:
                bulk_items = [BulkMemoryItem(**item) for item in items]
            except (ValidationError, TypeError) as e:
                return _with_latency(
                    json.dumps(
                        {
                            "error": {
                                "code": "INVALID_BATCH_ITEM",
                                "message": f"Invalid items — {e}",
                                "details": {"received_count": len(items)},
                            }
                        }
                    ),
                    t0,
                )
            bulk_data = BulkMemoryCreate(
                tenant_id=tenant_id,
                fleet_id=fleet_id,
                agent_id=agent_id,
                items=bulk_items,
                visibility=visibility,
            )
            # MCP transport doesn't surface ``X-Bulk-Attempt-Id``;
            # mint a server-side attempt id so each MCP-driven bulk
            # call still gets per-item idempotency. A retried MCP tool
            # call will use a different attempt id (so it isn't
            # idempotent across MCP retries) — the MCP transport is
            # unary and the loadtest finding (CAURA-602) doesn't apply
            # to it; the trade-off is acceptable to keep this path
            # simple. If a use case needs MCP retry idempotency, the
            # client can pass an explicit token via metadata.
            result = await create_memories_bulk(db, bulk_data, bulk_attempt_id=f"mcp:{uuid4()}")
            return _with_latency(_serialize(result), t0)
        except HTTPException as e:
            logger.warning("MCP tool error (%s): %s", e.status_code, e.detail)
            return _with_latency(_error_response(code_for_status(e.status_code), str(e.detail)), t0)


async def memclaw_manage(
    op: Annotated[str, Field(description="read|update|transition|delete|bulk_delete|lineage.")],
    memory_id: Annotated[str, Field(description="UUID. Required except for op=bulk_delete.")] = "",
    memory_ids: Annotated[
        list[str] | None,
        Field(description="op=bulk_delete: list of memory UUIDs (max 1000)."),
    ] = None,
    status: Annotated[str | None, Field(description="op=transition.")] = None,
    content: Annotated[str | None, Field(description="op=update.")] = None,
    memory_type: Annotated[str | None, Field(description="op=update.")] = None,
    weight: Annotated[float | None, Field(description="op=update; 0-1.")] = None,
    title: Annotated[str | None, Field(description="op=update.")] = None,
    metadata: Annotated[dict | None, Field(description="op=update.")] = None,
    source_uri: Annotated[str | None, Field(description="op=update.")] = None,
    agent_id: Annotated[str, Field(description="Caller agent.")] = "mcp-agent",
) -> str:
    """Per-memory lifecycle: read | update | transition | delete | bulk_delete | lineage.

    op=lineage walks the supersession chain for `memory_id` and returns
    {this, superseded_by, supersessors} — the older row this memory
    replaced (if any) and any newer rows that supersede this one.
    Mirrors the focused agent-facing view of REST `/memories/{id}/contradictions`.
    """
    t0 = time.perf_counter()
    if err := _check_auth():
        return err
    _valid_ops = {"read", "update", "transition", "delete", "bulk_delete", "lineage"}
    if op not in _valid_ops:
        return _with_latency(
            json.dumps(
                {
                    "error": {
                        "code": "INVALID_ARGUMENTS",
                        "message": f"Unknown op '{op}'. Expected one of: {sorted(_valid_ops)}.",
                        "details": {"op": op, "expected_ops": sorted(_valid_ops)},
                    }
                }
            ),
            t0,
        )
    # bulk_delete uses memory_ids (list); all other ops use memory_id (single UUID).
    # Validate accordingly so a missing memory_id on bulk_delete doesn't fail with
    # a misleading "Invalid UUID" error.
    if op != "bulk_delete":
        try:
            uid = UUID(memory_id)
        except ValueError:
            return _error_response("INVALID_ARGUMENTS", "Invalid memory_id — must be a valid UUID.")
    tenant_id = _get_tenant()
    agent_id = _get_agent_id() or agent_id

    async with _mcp_session() as db:
        try:
            if op == "bulk_delete":
                if not memory_ids:
                    return _with_latency(
                        _error_response(
                            "INVALID_ARGUMENTS", "op=bulk_delete requires non-empty 'memory_ids'."
                        ),
                        t0,
                    )
                if len(memory_ids) > 1000:
                    return _with_latency(
                        _error_response(
                            "INVALID_ARGUMENTS", f"op=bulk_delete capped at 1000 ids (got {len(memory_ids)})."
                        ),
                        t0,
                    )
                try:
                    uids = [UUID(i) for i in memory_ids]
                except ValueError as e:
                    return _with_latency(
                        _error_response("INVALID_ARGUMENTS", f"invalid UUID in memory_ids — {e}"), t0
                    )
                from datetime import UTC
                from datetime import datetime as _dt

                from sqlalchemy import update as _sa_update

                from common.models.memory import Memory as _Mem

                stmt = (
                    _sa_update(_Mem)
                    .where(
                        _Mem.tenant_id == tenant_id,
                        _Mem.id.in_(uids),
                        _Mem.deleted_at.is_(None),
                    )
                    .values(deleted_at=_dt.now(UTC), status="deleted")
                )
                result = await db.execute(stmt)
                await log_action(
                    db,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    action="bulk_delete",
                    resource_type="memory",
                    detail={"count": result.rowcount, "method": "by_ids", "via": "mcp"},
                )
                await db.commit()
                return _with_latency(json.dumps({"deleted": result.rowcount, "requested": len(uids)}), t0)
            if op == "lineage":
                from sqlalchemy import select as _sa_select

                from common.models.memory import Memory as _Mem

                this = await memory_repo.get_by_id_for_tenant(db, uid, tenant_id)
                if not this:
                    return _with_latency(_error_response("NOT_FOUND", "Memory not found."), t0)

                # The older memory this row replaced (if any). The
                # supersedes_id field points at the OLDER row (the
                # detector's "loser"); this row was the winner.
                superseded_by = None
                if this.supersedes_id:
                    older = await db.get(_Mem, this.supersedes_id)
                    if older and older.tenant_id == tenant_id and older.deleted_at is None:
                        superseded_by = {
                            "id": str(older.id),
                            "content_preview": older.content[:200],
                            "status": older.status,
                            "created_at": (older.created_at.isoformat() if older.created_at else None),
                        }

                # Newer rows whose supersedes_id points at this row
                # (this row was their "loser").
                stmt = (
                    _sa_select(_Mem)
                    .where(
                        _Mem.supersedes_id == uid,
                        _Mem.tenant_id == tenant_id,
                        _Mem.deleted_at.is_(None),
                    )
                    .order_by(_Mem.created_at.desc())
                )
                supersessors = [
                    {
                        "id": str(m.id),
                        "content_preview": m.content[:200],
                        "status": m.status,
                        "created_at": m.created_at.isoformat() if m.created_at else None,
                    }
                    for m in (await db.execute(stmt)).scalars().all()
                ]
                return _with_latency(
                    json.dumps(
                        {
                            "this": {
                                "id": str(this.id),
                                "status": this.status,
                                "supersedes_id": (str(this.supersedes_id) if this.supersedes_id else None),
                            },
                            "superseded_by": superseded_by,  # the OLDER row this replaced
                            "supersessors": supersessors,  # NEWER rows that replaced this
                        },
                        default=str,
                    ),
                    t0,
                )
            if op == "read":
                memory = await memory_repo.get_by_id_for_tenant(db, uid, tenant_id)
                if not memory:
                    return _with_latency(_error_response("NOT_FOUND", "Memory not found."), t0)
                return _with_latency(
                    json.dumps(
                        {
                            "id": str(memory.id),
                            "content": memory.content,
                            "memory_type": memory.memory_type,
                            "status": memory.status,
                            "weight": memory.weight,
                            "agent_id": memory.agent_id,
                            "fleet_id": memory.fleet_id,
                            "visibility": memory.visibility,
                            "title": getattr(memory, "title", None),
                            "created_at": memory.created_at.isoformat() if memory.created_at else None,
                            "last_recalled_at": (
                                memory.last_recalled_at.isoformat()
                                if getattr(memory, "last_recalled_at", None)
                                else None
                            ),
                            "recall_count": getattr(memory, "recall_count", 0),
                            "deleted_at": (
                                memory.deleted_at.isoformat() if getattr(memory, "deleted_at", None) else None
                            ),
                            "metadata": getattr(memory, "metadata_", None),
                        },
                        default=str,
                    ),
                    t0,
                )
            if op == "transition":
                if not status:
                    return _with_latency(
                        _error_response("INVALID_ARGUMENTS", "op=transition requires 'status'."), t0
                    )
                if status not in MEMORY_STATUSES:
                    return _with_latency(
                        _error_response(
                            "INVALID_ARGUMENTS",
                            f"Invalid status '{status}'. Must be one of: {', '.join(MEMORY_STATUSES)}",
                        ),
                        t0,
                    )
                memory = await memory_repo.get_by_id_for_tenant(db, uid, tenant_id)
                if not memory:
                    return _with_latency(_error_response("NOT_FOUND", "Memory not found."), t0)
                old_status = memory.status
                await memory_repo.update_status(db, uid, status)
                await log_action(
                    db,
                    tenant_id=tenant_id,
                    agent_id=memory.agent_id,
                    action="status_update",
                    resource_type="memory",
                    resource_id=uid,
                    detail={"old_status": old_status, "new_status": status},
                )
                await db.commit()
                return _with_latency(f"Memory {memory_id} status updated: {old_status} -> {status}", t0)
            if op == "update":
                fields: dict = {}
                if content is not None:
                    fields["content"] = content
                if memory_type is not None:
                    fields["memory_type"] = memory_type
                if weight is not None:
                    fields["weight"] = weight
                if title is not None:
                    fields["title"] = title
                if status is not None:
                    fields["status"] = status
                if metadata is not None:
                    fields["metadata"] = metadata
                if source_uri is not None:
                    fields["source_uri"] = source_uri
                if not fields:
                    return _with_latency(
                        "Error: No fields to update. Provide at least one field to change.", t0
                    )
                await check_and_increment(db, tenant_id, "write")
                result = await update_memory(db, uid, tenant_id, MemoryUpdate(**fields), agent_id=agent_id)
                return _with_latency(_serialize(result), t0)
            # op == "delete"
            await soft_delete_memory(db, uid, tenant_id)
            return _with_latency(f"Memory {memory_id} deleted.", t0)
        except HTTPException as e:
            logger.warning("MCP tool error (%s): %s", e.status_code, e.detail)
            return _with_latency(_error_response(code_for_status(e.status_code), str(e.detail)), t0)


async def memclaw_entity_get(
    entity_id: Annotated[str, Field(description="The UUID of the entity to look up.")],
) -> str:
    t0 = time.perf_counter()
    if err := _check_auth():
        return err
    try:
        uid = UUID(entity_id)
    except ValueError:
        return _error_response("INVALID_ARGUMENTS", "Invalid entity_id — must be a valid UUID.")

    async with _mcp_session() as db:
        result = await get_entity(db, uid, _get_tenant())
        text = "Entity not found." if not result else _serialize(result)
        return _with_latency(text, t0)


async def memclaw_tune(
    agent_id: Annotated[str, Field(description="Caller agent.")] = "mcp-agent",
    top_k: Annotated[int | None, Field(description="1-20.")] = None,
    min_similarity: Annotated[float | None, Field(description="0.1-0.9.")] = None,
    fts_weight: Annotated[float | None, Field(description="0=semantic, 1=keyword.")] = None,
    freshness_floor: Annotated[float | None, Field(description="0-1.")] = None,
    freshness_decay_days: Annotated[int | None, Field(description="7-730.")] = None,
    recall_boost_cap: Annotated[float | None, Field(description="1-3.")] = None,
    recall_decay_window_days: Annotated[int | None, Field(description="7-365.")] = None,
    graph_max_hops: Annotated[int | None, Field(description="0-3.")] = None,
    similarity_blend: Annotated[float | None, Field(description="0-1.")] = None,
) -> str:
    t0 = time.perf_counter()
    if err := _check_auth():
        return err
    tenant_id = _get_tenant()
    agent_id = _get_agent_id() or agent_id

    from core_api.schemas import SearchProfileUpdate

    try:
        profile = SearchProfileUpdate(
            top_k=top_k,
            min_similarity=min_similarity,
            fts_weight=fts_weight,
            freshness_floor=freshness_floor,
            freshness_decay_days=freshness_decay_days,
            recall_boost_cap=recall_boost_cap,
            recall_decay_window_days=recall_decay_window_days,
            graph_max_hops=graph_max_hops,
            similarity_blend=similarity_blend,
        )
    except (ValidationError, ValueError) as e:
        return _with_latency(_error_response("INVALID_ARGUMENTS", f"{e}"), t0)

    updates = profile.model_dump(exclude_none=True)
    async with _mcp_session() as db:
        try:
            from core_api.repositories import agent_repo
            from core_api.services.agent_service import get_or_create_agent

            agent = await get_or_create_agent(db, tenant_id, agent_id)
            current = (agent.get("search_profile") if isinstance(agent, dict) else agent.search_profile) or {}
            if updates:
                current.update(updates)
                from core_api.services.organization_settings import validate_search_profile

                current = validate_search_profile(current)
                await agent_repo.update_search_profile(db, agent.id, current)
                await db.commit()
            return _with_latency(json.dumps({"agent_id": agent_id, "search_profile": current}, indent=2), t0)
        except HTTPException as e:
            logger.warning("MCP tool error (%s): %s", e.status_code, e.detail)
            return _with_latency(_error_response(code_for_status(e.status_code), str(e.detail)), t0)


# ---------------------------------------------------------------------------
# Consolidated tools: doc CRUD, list, knowledge-layer placeholders
# ---------------------------------------------------------------------------


async def memclaw_doc(
    op: Annotated[str, Field(description="write|read|query|delete|list_collections|search.")],
    collection: Annotated[
        str | None,
        Field(
            description="Collection. Required for write|read|query|delete|search; omitted for list_collections."
        ),
    ] = None,
    doc_id: Annotated[str | None, Field(description="op=write|read|delete.")] = None,
    data: Annotated[dict | None, Field(description="op=write.")] = None,
    where: Annotated[dict | None, Field(description="op=query.")] = None,
    order_by: Annotated[str | None, Field(description="op=query.")] = None,
    order: Annotated[str, Field(description="op=query: asc|desc.")] = "asc",
    limit: Annotated[int, Field(description="op=query.")] = 20,
    offset: Annotated[int, Field(description="op=query.")] = 0,
    agent_id: Annotated[str, Field(description="Caller agent.")] = "mcp-agent",
    fleet_id: Annotated[
        str | None,
        Field(description="op=write; optional scoping filter for op=list_collections|search."),
    ] = None,
    embed_field: Annotated[
        str | None,
        Field(
            description=(
                "op=write: JSON field in `data` whose text value should be embedded "
                "for semantic search. Omit to skip indexing (doc won't appear in op=search)."
            ),
        ),
    ] = None,
    query: Annotated[str | None, Field(description="op=search: natural-language query.")] = None,
    top_k: Annotated[int, Field(description="op=search: max results (1-50).")] = 5,
) -> str:
    """Structured-document CRUD. Op-dispatched. Replaces the 4 prior
    `memclaw_doc_*` tools."""
    t0 = time.perf_counter()
    if err := _check_auth():
        return err
    _valid_ops = {"write", "read", "query", "delete", "list_collections", "search"}
    if op not in _valid_ops:
        return _with_latency(
            json.dumps(
                {
                    "error": {
                        "code": "INVALID_ARGUMENTS",
                        "message": f"Unknown op '{op}'. Expected one of: {sorted(_valid_ops)}.",
                        "details": {"op": op, "expected_ops": sorted(_valid_ops)},
                    }
                }
            ),
            t0,
        )
    # `collection` is required for write/read/query/delete. It is optional
    # for list_collections (by design) and for search (where omitting it
    # means "search across every collection in this tenant" — the broad
    # strategy; supply collection to scope the search to just one).
    if op not in {"list_collections", "search"} and not collection:
        return _with_latency(_error_response("INVALID_ARGUMENTS", f"op={op} requires 'collection'."), t0)
    tenant_id = _get_tenant()
    agent_id = _get_agent_id() or agent_id

    from core_api.repositories import document_repo

    async with _mcp_session() as db:
        try:
            if op == "list_collections":
                rows = await document_repo.list_collections(db, tenant_id=tenant_id, fleet_id=fleet_id)
                return _with_latency(
                    json.dumps(
                        {
                            "collections": [{"name": name, "count": count} for name, count in rows],
                            "count": len(rows),
                        }
                    ),
                    t0,
                )
            if op == "write":
                if not doc_id:
                    return _with_latency(
                        _error_response("INVALID_ARGUMENTS", "op=write requires 'doc_id'."), t0
                    )
                if data is None:
                    return _with_latency(
                        _error_response("INVALID_ARGUMENTS", "op=write requires 'data'."), t0
                    )
                # Optional semantic indexing: embed data[embed_field] so the
                # doc participates in op=search. Missing/empty source field is
                # a caller mistake — fail loud rather than silently skip.
                embedding: list[float] | None = None
                if embed_field:
                    source = data.get(embed_field)
                    if not isinstance(source, str) or not source.strip():
                        return _with_latency(
                            _error_response(
                                "INVALID_ARGUMENTS",
                                f"op=write embed_field "
                                f"'{embed_field}' not found in data or not a non-empty string.",
                            ),
                            t0,
                        )
                    from common.embedding import get_embedding

                    embedding = await get_embedding(source)
                    if embedding is None:
                        return _with_latency(
                            "Error: embedding provider returned no vector "
                            "(check provider config / quota). Write aborted.",
                            t0,
                        )
                await check_and_increment(db, tenant_id, "write")
                row = await document_repo.upsert_returning_xmax(
                    db,
                    tenant_id=tenant_id,
                    fleet_id=fleet_id,
                    collection=collection,
                    doc_id=doc_id,
                    data=data,
                    embedding=embedding,
                )
                if row is None:
                    return _with_latency(
                        _error_response("INTERNAL_ERROR", "document upsert returned no rows"), t0
                    )
                await db.commit()
                # `text("xmax")` is unlabeled in SQLAlchemy ≥ 2; access by
                # tuple position. Returning columns are: id, created_at,
                # updated_at, xmax — so xmax sits at index 3.
                is_new = int(row[3]) == 0
                return _with_latency(
                    json.dumps(
                        {
                            "ok": True,
                            "collection": collection,
                            "doc_id": doc_id,
                            "action": "created" if is_new else "updated",
                            "indexed": embedding is not None,
                        }
                    ),
                    t0,
                )
            if op == "read":
                if not doc_id:
                    return _with_latency(
                        _error_response("INVALID_ARGUMENTS", "op=read requires 'doc_id'."), t0
                    )
                doc = await document_repo.get_by_doc_id(
                    db, tenant_id=tenant_id, collection=collection, doc_id=doc_id
                )
                if not doc:
                    return _with_latency(f"Not found: {collection}/{doc_id}", t0)
                return _with_latency(
                    json.dumps(
                        {
                            "collection": doc.collection,
                            "doc_id": doc.doc_id,
                            "data": doc.data,
                            "updated_at": doc.updated_at.isoformat(),
                        },
                        default=str,
                    ),
                    t0,
                )
            if op == "query":
                docs = await document_repo.query(
                    db,
                    tenant_id=tenant_id,
                    collection=collection,
                    where=where or {},
                    order_by=order_by,
                    order=order,
                    limit=min(limit, 100),
                    offset=offset,
                )
                items = [{"doc_id": d.doc_id, "data": d.data} for d in docs]
                return _with_latency(
                    json.dumps(
                        {"collection": collection, "count": len(items), "results": items},
                        default=str,
                    ),
                    t0,
                )
            if op == "search":
                if not query or not query.strip():
                    return _with_latency(
                        _error_response("INVALID_ARGUMENTS", "op=search requires a non-empty 'query'."), t0
                    )
                from common.embedding import get_embedding

                query_embedding = await get_embedding(query)
                if query_embedding is None:
                    return _with_latency(
                        "Error: embedding provider returned no vector "
                        "(check provider config / quota). Search aborted.",
                        t0,
                    )
                capped_top_k = max(1, min(top_k, 50))
                pairs = await document_repo.search(
                    db,
                    tenant_id=tenant_id,
                    collection=collection,  # None = span every collection (broad)
                    query_embedding=query_embedding,
                    top_k=capped_top_k,
                    fleet_id=fleet_id,
                )
                # Always include `collection` per-row. When collection is
                # omitted (broad search) the caller needs it to follow up
                # with op=read. Zero cost when scoped; avoids a conditional
                # response shape.
                items = [
                    {
                        "collection": d.collection,
                        "doc_id": d.doc_id,
                        "data": d.data,
                        "similarity": round(sim, 4),
                    }
                    for d, sim in pairs
                ]
                return _with_latency(
                    json.dumps(
                        {
                            "collection": collection,  # None if broad search
                            "count": len(items),
                            "results": items,
                        },
                        default=str,
                    ),
                    t0,
                )
            # op == "delete"
            if not doc_id:
                return _with_latency(_error_response("INVALID_ARGUMENTS", "op=delete requires 'doc_id'."), t0)
            from sqlalchemy import delete as sa_delete

            from common.models.document import Document

            stmt = (
                sa_delete(Document)
                .where(
                    Document.tenant_id == tenant_id,
                    Document.collection == collection,
                    Document.doc_id == doc_id,
                )
                .returning(Document.id)
            )
            result = await db.execute(stmt)
            deleted_id = result.scalar_one_or_none()
            if not deleted_id:
                return _with_latency(
                    json.dumps({"error": f"Document '{doc_id}' not found in collection '{collection}'"}),
                    t0,
                )
            await db.commit()
            return _with_latency(
                json.dumps({"ok": True, "collection": collection, "doc_id": doc_id, "deleted": True}),
                t0,
            )
        except HTTPException as e:
            logger.warning("MCP tool error (%s): %s", e.status_code, e.detail)
            return _with_latency(_error_response(code_for_status(e.status_code), str(e.detail)), t0)
        except Exception as e:
            logger.error("MCP doc op=%s error: %s", op, e, exc_info=True)
            return _with_latency(_error_response("INTERNAL_ERROR", str(e)), t0)


async def memclaw_list(
    agent_id: Annotated[str, Field(description="Caller agent.")] = "mcp-agent",
    scope: Annotated[
        str,
        Field(
            description="agent|fleet|all. 'agent' (default) = your memories only (trust ≥ 1). 'fleet'/'all' = cross-agent (trust ≥ 2)."
        ),
    ] = "agent",
    fleet_id: Annotated[str | None, Field(description="Fleet.")] = None,
    written_by: Annotated[str | None, Field(description="Author.")] = None,
    memory_type: Annotated[str | None, Field(description="Type.")] = None,
    status: Annotated[str | None, Field(description="Status.")] = None,
    weight_min: Annotated[float | None, Field(description="Min weight 0-1.")] = None,
    weight_max: Annotated[float | None, Field(description="Max weight 0-1.")] = None,
    created_after: Annotated[str | None, Field(description="ISO8601.")] = None,
    created_before: Annotated[str | None, Field(description="ISO8601.")] = None,
    sort: Annotated[str, Field(description="created_at|weight|recall_count.")] = "created_at",
    order: Annotated[str, Field(description="asc|desc.")] = "desc",
    limit: Annotated[int, Field(description="1-50.")] = 25,
    cursor: Annotated[str | None, Field(description="Pagination cursor.")] = None,
    include_deleted: Annotated[bool, Field(description="Trust-3 only.")] = False,
) -> str:
    """Non-semantic memory enumeration: filter, sort, paginate by metadata.
    scope='agent' (default) requires trust ≥ 1; scope='fleet'/'all' requires
    trust ≥ 2. Trust 3 unlocks ``include_deleted``."""
    t0 = time.perf_counter()
    if err := _check_auth():
        return err
    if scope not in VALID_SCOPES:
        return _error_response("INVALID_ARGUMENTS", f"Invalid scope '{scope}'. Must be: agent, fleet, all.")
    if memory_type and memory_type not in MEMORY_TYPES:
        return _error_response(
            "INVALID_ARGUMENTS",
            f"Invalid memory_type '{memory_type}'. Must be one of: {', '.join(MEMORY_TYPES)}",
        )
    if status and status not in MEMORY_STATUSES:
        return _error_response(
            "INVALID_ARGUMENTS", f"Invalid status '{status}'. Must be one of: {', '.join(MEMORY_STATUSES)}"
        )
    if sort not in {"created_at", "weight", "recall_count"}:
        return _error_response(
            "INVALID_ARGUMENTS", f"Invalid sort '{sort}'. Must be one of: created_at, weight, recall_count."
        )
    if order not in {"asc", "desc"}:
        return _error_response("INVALID_ARGUMENTS", "order must be 'asc' or 'desc'.")
    if cursor and (sort != "created_at" or order != "desc"):
        return _error_response(
            "INVALID_ARGUMENTS", "cursor pagination requires sort=created_at and order=desc."
        )
    capped_limit = max(1, min(int(limit), 50))

    from datetime import datetime as _dt

    from core_api.pagination import decode_cursor, encode_cursor
    from core_api.services.memory_service import _memory_to_out

    tenant_id = _get_tenant()
    agent_id = _get_agent_id() or agent_id

    if scope == "agent" and written_by is not None and written_by != agent_id:
        return _error_response(
            "INVALID_ARGUMENTS",
            f"written_by must be omitted or match your own agent_id ('{agent_id}') when scope='agent'.",
        )

    # Dynamic trust: scope='agent' requires trust ≥ 1, 'fleet'/'all' requires ≥ 2.
    min_level = 1 if scope == "agent" else 2

    async with _mcp_session() as db:
        trust, _, terr = await _require_trust(db, tenant_id, agent_id, min_level=min_level)
        if terr:
            return _with_latency(_error_response("FORBIDDEN", parse_trust_error(terr)), t0)

        # scope='agent': force written_by to the caller's agent_id so they
        # can only see their own memories regardless of other filters.
        effective_written_by = agent_id if scope == "agent" else written_by

        # include_deleted is silently ignored below trust 3
        effective_include_deleted = include_deleted and trust >= 3

        # Parse ISO date strings (validated early to avoid repo-level errors).
        ts_after = ts_before = None
        if created_after:
            try:
                ts_after = _dt.fromisoformat(created_after)
            except ValueError:
                return _with_latency(
                    _error_response("INVALID_ARGUMENTS", "created_after must be ISO8601."), t0
                )
        if created_before:
            try:
                ts_before = _dt.fromisoformat(created_before)
            except ValueError:
                return _with_latency(
                    _error_response("INVALID_ARGUMENTS", "created_before must be ISO8601."), t0
                )

        c_ts = c_id = None
        if cursor:
            try:
                c_ts, c_id = decode_cursor(cursor)
            except Exception:
                return _with_latency(_error_response("INVALID_ARGUMENTS", "Invalid cursor."), t0)

        rows = await memory_repo.list_by_filters(
            db,
            tenant_id=tenant_id,
            caller_agent_id=agent_id,
            fleet_id=fleet_id,
            written_by=effective_written_by,
            memory_type=memory_type,
            status=status,
            weight_min=weight_min,
            weight_max=weight_max,
            created_after=ts_after,
            created_before=ts_before,
            include_deleted=effective_include_deleted,
            sort=sort,
            order=order,
            limit=capped_limit,
            cursor_ts=c_ts,
            cursor_id=c_id,
        )
        has_more = len(rows) > capped_limit
        items = [_memory_to_out(m).model_dump(mode="json") for m in rows[:capped_limit]]
        next_cursor = None
        if has_more and rows:
            last = rows[capped_limit - 1]
            next_cursor = encode_cursor(last.created_at, last.id)
        return _with_latency(
            json.dumps(
                {"count": len(items), "results": items, "next_cursor": next_cursor, "scope": scope},
                default=str,
            ),
            t0,
        )


async def memclaw_stats(
    scope: Annotated[
        str,
        Field(
            description="agent|fleet|all. 'agent' (default) = your memories only (trust ≥ 1). 'fleet'/'all' = aggregate across agents (trust ≥ 2)."
        ),
    ] = "agent",
    fleet_id: Annotated[str | None, Field(description="Filter by fleet.")] = None,
    agent_id: Annotated[str, Field(description="Caller agent.")] = "mcp-agent",
    memory_type: Annotated[str | None, Field(description="Filter by type.")] = None,
    status: Annotated[str | None, Field(description="Filter by status.")] = None,
) -> str:
    """Aggregate counts: total plus breakdowns by type, agent, status.
    scope='agent' (default) requires trust ≥ 1; scope='fleet'/'all' requires
    trust ≥ 2. scope='agent' counts only memories visible to the caller (mirrors
    memclaw_list visibility scoping); broader scopes drop the per-caller filter."""
    t0 = time.perf_counter()
    if err := _check_auth():
        return err
    if scope not in VALID_SCOPES:
        return _with_latency(
            _error_response("INVALID_ARGUMENTS", f"Invalid scope '{scope}'. Must be: agent, fleet, all."),
            t0,
        )
    if memory_type and memory_type not in MEMORY_TYPES:
        return _with_latency(
            _error_response(
                "INVALID_ARGUMENTS",
                f"Invalid memory_type '{memory_type}'. Must be one of: {', '.join(MEMORY_TYPES)}",
            ),
            t0,
        )
    if status and status not in MEMORY_STATUSES:
        return _with_latency(
            _error_response(
                "INVALID_ARGUMENTS",
                f"Invalid status '{status}'. Must be one of: {', '.join(MEMORY_STATUSES)}",
            ),
            t0,
        )

    tenant_id = _get_tenant()
    agent_id = _get_agent_id() or agent_id
    min_level = 1 if scope == "agent" else 2

    async with _mcp_session() as db:
        trust, _, terr = await _require_trust(db, tenant_id, agent_id, min_level=min_level)
        if terr:
            return _with_latency(_error_response("FORBIDDEN", parse_trust_error(terr)), t0)

        # scope='agent' filters to caller's own memories (mirrors memclaw_list);
        # scope='fleet'/'all' drops the per-caller filter so cross-agent
        # aggregates surface — fleet_id (if supplied) still narrows the pool.
        effective_agent_id = agent_id if scope == "agent" else None

        from core_api.services.memory_stats import compute_memory_stats

        try:
            stats = await compute_memory_stats(
                db,
                tenant_id=tenant_id,
                fleet_id=fleet_id,
                agent_id=effective_agent_id,
                memory_type=memory_type,
                status=status,
            )
            return _with_latency(json.dumps({**stats, "scope": scope}, default=str), t0)
        except Exception as e:
            logger.exception("Unhandled error in memclaw_stats")
            return _with_latency(_error_response("INTERNAL_ERROR", str(e)), t0)


async def memclaw_share_skill(
    name: Annotated[
        str, Field(description="Slug: [a-z0-9._-], 1-100 chars. Used as upsert key + directory.")
    ],
    description: Annotated[str, Field(description="One-line summary (1-500 chars).")],
    content: Annotated[str, Field(description="Full SKILL.md markdown.")],
    target_fleet_id: Annotated[str, Field(description="Fleet the skill is scoped to.")],
    install_on_fleet: Annotated[
        bool,
        Field(
            description="False (default): publish to catalog only. True: also push to every node in target_fleet_id (auto-install)."
        ),
    ] = False,
    agent_id: Annotated[str, Field(description="Author agent.")] = "mcp-agent",
    target_agent_ids: Annotated[
        list[str] | None, Field(description="Recipient agent_ids (informational).")
    ] = None,
    version: Annotated[int, Field(description="Version (default 1).")] = 1,
) -> str:
    """Share a SKILL.md. Default publishes to catalog (recipients pull on demand);
    set install_on_fleet=true to auto-install on every fleet node. Trust ≥ 1."""
    t0 = time.perf_counter()
    if err := _check_auth():
        return err

    tenant_id = _get_tenant()
    agent_id = _get_agent_id() or agent_id

    async with _mcp_session() as db:
        trust, _, terr = await _require_trust(db, tenant_id, agent_id, min_level=1)
        if terr:
            return _with_latency(_error_response("FORBIDDEN", parse_trust_error(terr)), t0)

        from core_api.services.skill_service import share_skill

        try:
            result = await share_skill(
                db=db,
                tenant_id=tenant_id,
                name=name,
                description=description,
                content=content,
                target_fleet_id=target_fleet_id,
                install_on_fleet=install_on_fleet,
                author_agent_id=agent_id,
                target_agent_ids=target_agent_ids,
                version=version,
            )
        except ValueError as e:
            return _with_latency(_error_response("INVALID_ARGUMENTS", str(e)), t0)
        except Exception as e:
            logger.exception("Unhandled error in memclaw_share_skill")
            return _with_latency(_error_response("INTERNAL_ERROR", str(e)), t0)

        return _with_latency(json.dumps(result, default=str), t0)


async def memclaw_unshare_skill(
    name: Annotated[str, Field(description="Skill name (slug, must match the share).")],
    unshare_from_fleet: Annotated[
        bool,
        Field(
            description="False (default): remove from catalog only. True: also rm SKILL.md on every fleet node (requires target_fleet_id)."
        ),
    ] = False,
    target_fleet_id: Annotated[
        str | None,
        Field(description="Required when unshare_from_fleet=true."),
    ] = None,
    agent_id: Annotated[str, Field(description="Caller agent.")] = "mcp-agent",
) -> str:
    """Remove a shared skill. Default removes from catalog only;
    set unshare_from_fleet=true to also delete from every fleet node. Trust ≥ 1."""
    t0 = time.perf_counter()
    if err := _check_auth():
        return err

    tenant_id = _get_tenant()
    agent_id = _get_agent_id() or agent_id

    async with _mcp_session() as db:
        trust, _, terr = await _require_trust(db, tenant_id, agent_id, min_level=1)
        if terr:
            return _with_latency(_error_response("FORBIDDEN", parse_trust_error(terr)), t0)

        from core_api.services.skill_service import unshare_skill

        try:
            result = await unshare_skill(
                db=db,
                tenant_id=tenant_id,
                name=name,
                unshare_from_fleet=unshare_from_fleet,
                target_fleet_id=target_fleet_id,
            )
        except ValueError as e:
            return _with_latency(_error_response("INVALID_ARGUMENTS", str(e)), t0)
        except Exception as e:
            logger.exception("Unhandled error in memclaw_unshare_skill")
            return _with_latency(_error_response("INTERNAL_ERROR", str(e)), t0)

        return _with_latency(json.dumps(result, default=str), t0)


# ---------------------------------------------------------------------------
# Intelligence tools (Karpathy Loop)
# ---------------------------------------------------------------------------


async def memclaw_insights(
    focus: Annotated[
        str,
        Field(
            description="contradictions|failures|stale|divergence|patterns|discover.",
        ),
    ],
    scope: Annotated[str, Field(description="agent|fleet|all.")] = "agent",
    fleet_id: Annotated[str | None, Field(description="Required when scope='fleet'.")] = None,
    agent_id: Annotated[str, Field(description="Caller agent.")] = "mcp-agent",
) -> str:
    """Analyze the memory store for patterns, contradictions, stale knowledge,
    or unexpected clusters; persist findings as ``insight`` memories.
    Consolidates onto the Karpathy Loop reflection step.
    scope='agent' (default) requires trust ≥ 1; scope='fleet'/'all' requires
    trust ≥ 2.
    """
    t0 = time.perf_counter()
    if err := _check_auth():
        return err
    tenant_id = _get_tenant()
    agent_id = _get_agent_id() or agent_id

    # Pre-validate inputs before consuming rate-limit budget.
    if focus not in INSIGHTS_FOCUS_MODES:
        return _with_latency(
            _error_response(
                "INVALID_ARGUMENTS",
                f"Invalid focus '{focus}'. Must be one of: {', '.join(INSIGHTS_FOCUS_MODES)}",
            ),
            t0,
        )
    if scope not in VALID_SCOPES:
        return _with_latency(
            _error_response("INVALID_ARGUMENTS", f"Invalid scope '{scope}'. Must be: agent, fleet, all"), t0
        )
    if scope == "fleet" and not fleet_id:
        return _with_latency(
            _error_response("INVALID_ARGUMENTS", "fleet_id is required when scope is 'fleet'."), t0
        )
    if focus == "divergence" and scope == "agent":
        return _with_latency(
            _error_response("INVALID_ARGUMENTS", "Focus 'divergence' requires scope='fleet' or scope='all'."),
            t0,
        )

    # Dynamic trust: scope='agent' requires trust ≥ 1, 'fleet'/'all' requires ≥ 2.
    min_level = 1 if scope == "agent" else 2

    async with _mcp_session() as db:
        # Mirror the REST insights gate: ``require_trust`` soft-passes a
        # missing Agent row at ``DEFAULT_TRUST_LEVEL`` (read-only ergonomics
        # — see ``memclaw_list`` below for the intended consumer), but
        # this handler persists insight memories + audit-log rows keyed
        # to ``agent_id``. Without a registered row backing the name,
        # attribution becomes unverifiable, so re-block unregistered
        # agents on the write path. ``terr`` is None for the soft-pass
        # case, so without the explicit ``not_found`` check below the
        # fabricated id would fall through and write.
        _, not_found, terr = await _require_trust(db, tenant_id, agent_id, min_level=min_level)
        if not_found:
            return _with_latency(
                f"Error (403): Agent '{agent_id}' is not registered. "
                "Register the agent by writing one memory first.",
                t0,
            )
        if terr:
            return _with_latency(_error_response("FORBIDDEN", parse_trust_error(terr)), t0)
        try:
            await check_and_increment(db, tenant_id, "insights")
            from core_api.services.insights_service import generate_insights

            result = await generate_insights(
                db,
                tenant_id=tenant_id,
                focus=focus,
                scope=scope,
                fleet_id=fleet_id,
                agent_id=agent_id,
            )
            return _with_latency(json.dumps(result, indent=2, default=str), t0)
        except HTTPException as e:
            return _with_latency(_error_response(code_for_status(e.status_code), str(e.detail)), t0)
        except Exception as e:
            logger.exception("Unhandled error in memclaw_insights")
            return _with_latency(_error_response("INTERNAL_ERROR", str(e)), t0)


async def memclaw_evolve(
    outcome: Annotated[str, Field(description="Natural-language description of what happened.")],
    outcome_type: Annotated[str, Field(description="success|failure|partial.")],
    related_ids: Annotated[
        list[str] | None,
        Field(description="Memory UUIDs that influenced the action."),
    ] = None,
    scope: Annotated[str, Field(description="agent|fleet|all.")] = "agent",
    agent_id: Annotated[str, Field(description="Caller agent.")] = "mcp-agent",
    fleet_id: Annotated[str | None, Field(description="Required when scope='fleet'.")] = None,
) -> str:
    """Record a real-world outcome against the memories that influenced the
    action: adjust weights, generate preventive rules on failure. Closes the
    Karpathy Loop feedback edge.

    scope='agent' (default) requires trust ≥ 1 and limits adjustments to
    memories the caller wrote. scope='fleet'/'all' requires trust ≥ 2.
    """
    t0 = time.perf_counter()
    if err := _check_auth():
        return err
    tenant_id = _get_tenant()
    agent_id = _get_agent_id() or agent_id

    # Pre-validate inputs before consuming rate-limit budget.
    if outcome_type not in EVOLVE_OUTCOME_TYPES:
        return _with_latency(
            _error_response(
                "INVALID_ARGUMENTS",
                f"Invalid outcome_type '{outcome_type}'. Must be one of: {', '.join(EVOLVE_OUTCOME_TYPES)}",
            ),
            t0,
        )
    if not outcome or not outcome.strip():
        return _with_latency(
            _error_response("INVALID_ARGUMENTS", "outcome must be a non-empty description."), t0
        )
    if scope not in VALID_SCOPES:
        return _with_latency(
            _error_response("INVALID_ARGUMENTS", f"Invalid scope '{scope}'. Must be: agent, fleet, all."), t0
        )
    if scope == "fleet" and not fleet_id:
        return _with_latency(
            _error_response("INVALID_ARGUMENTS", "fleet_id is required when scope is 'fleet'."), t0
        )

    # Dynamic trust: scope='agent' requires trust ≥ 1, 'fleet'/'all' requires ≥ 2.
    min_level = 1 if scope == "agent" else 2

    async with _mcp_session() as db:
        # Mirror the REST evolve gate (and ``memclaw_insights`` above):
        # block unregistered agents on the write path so the
        # outcome/rule memories + audit-log rows have a real registered
        # ``agent_id`` backing them. Soft-pass remains in
        # ``require_trust`` itself for the read-only ``memclaw_list``
        # below.
        _, not_found, terr = await _require_trust(db, tenant_id, agent_id, min_level=min_level)
        if not_found:
            return _with_latency(
                f"Error (403): Agent '{agent_id}' is not registered. "
                "Register the agent by writing one memory first.",
                t0,
            )
        if terr:
            return _with_latency(_error_response("FORBIDDEN", parse_trust_error(terr)), t0)
        try:
            await check_and_increment(db, tenant_id, "evolve")
            from core_api.services.evolve_service import report_outcome

            result = await report_outcome(
                db,
                tenant_id=tenant_id,
                outcome=outcome,
                outcome_type=outcome_type,
                related_ids=related_ids,
                scope=scope,
                agent_id=agent_id,
                fleet_id=fleet_id,
            )
            return _with_latency(json.dumps(result, indent=2, default=str), t0)
        except HTTPException as e:
            return _with_latency(_error_response(code_for_status(e.status_code), str(e.detail)), t0)
        except Exception as e:
            logger.exception("Unhandled error in memclaw_evolve")
            return _with_latency(_error_response("INTERNAL_ERROR", str(e)), t0)


# ── Mountable app + lifespan ──

_mcp_starlette_app = mcp.streamable_http_app()


def get_mcp_app() -> ASGIApp:
    return MCPAuthMiddleware(_mcp_starlette_app)


@contextlib.asynccontextmanager
async def mcp_lifespan():
    """Run MCP session manager lifecycle. Enter during FastAPI lifespan."""
    async with mcp.session_manager.run():
        yield


# ── SoT registration ──────────────────────────────────────────────────────
# Triggers loading of every `core_api.tools.memclaw_*.py` spec module. Each
# spec module registers itself in the REGISTRY and calls `mcp_register(mcp, spec)`
# to wire the handler to FastMCP. This import must run AFTER the 16 handler
# functions above are defined — spec modules reference them via
# `core_api.mcp_server.memclaw_X` attribute lookup.
# The `noqa: E402,F401` silences "module-level import not at top" and
# "imported but unused" — both are intentional.
from core_api import tools  # noqa: F401
