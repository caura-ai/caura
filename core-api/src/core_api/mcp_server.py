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
import re
import time
from typing import Annotated, cast
from uuid import UUID, uuid4

import httpx
from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from pydantic import Field, ValidationError
from sqlalchemy import text as sa_text
from starlette.types import ASGIApp, Receive, Scope, Send

from core_api.auth import get_admin_key
from core_api.clients.storage_client import KeystoneUpsertPayload, get_storage_client
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
from core_api.services.agent_service import enforce_fleet_write
from core_api.services.audit_service import log_action, log_cross_tenant_read
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
from core_api.trust_utils import effective_keystone_min_trust, keystone_min_trust

logger = logging.getLogger(__name__)

# ── Auth via context vars ──

_tenant_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("mcp_tenant_id")
_agent_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("mcp_agent_id", default=None)
# True iff X-Tenant-ID arrived as a request header (gateway-routed). On that
# path the gateway is the source of truth for identity; falling back to the
# literal "mcp-agent" tool-param default would silently attribute every write
# from a tenant-key holder to a single shared identity (friction §2.8 / Stage 6b).
_via_gateway_var: contextvars.ContextVar[bool] = contextvars.ContextVar("mcp_via_gateway", default=False)
# Cross-tenant read set plumbed from X-Readable-Tenant-IDs (CSV). Empty list
# means single-tenant key (reads pinned to ``_tenant_id_var``). When populated
# the home tenant_id is the first element and writes still go there.
_readable_tenant_ids_var: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "mcp_readable_tenant_ids", default=None
)
_scopes_var: contextvars.ContextVar[set[str] | None] = contextvars.ContextVar("mcp_scopes", default=None)

_UNAUTH = "__unauthenticated__"
_ADMIN = "__admin__"
_NO_AUTH = "__no_auth__"
_DEFAULT_AGENT_ID = "mcp-agent"


def _error_response(code: str, message: str, **details) -> str:
    """Return the canonical MCP error envelope as a JSON string.

    Shape matches the REST surface (see ``core_api.errors.make_error_payload``):
    ``{"error": {"code": "...", "message": "...", "details": {...}}}``.

    The string is wrapped into a ``CallToolResult(isError=True)`` before
    leaving the tool — either by ``_with_latency`` (which detects the
    error shape) or by ``_as_error_result`` for pre-tool returns that
    skip the latency stamp. Tool callsites keep returning plain strings;
    the wrap happens at one of those two chokepoints (CAURA-000,
    FRICTION-REPORT-V3 B2 — clients literal-reading the MCP spec were
    seeing ``isError=False`` on every gateway-side refusal).
    """
    from core_api.errors import make_error_payload

    payload = make_error_payload(code, message, details=details if details else None)
    return json.dumps(payload, default=str)


def _as_error_result(envelope: str) -> CallToolResult:
    """Wrap a ``_error_response`` JSON envelope into a CallToolResult
    with ``isError=True``. The envelope JSON is preserved verbatim in
    a single TextContent so callers reading ``result.content[0].text``
    still get the structured payload. Used for the pre-tool / auth
    return paths that don't run through ``_with_latency``.
    """
    return CallToolResult(
        content=[TextContent(type="text", text=envelope)],
        isError=True,
    )


# Pre-tool auth errors (returned directly, NOT through _with_latency, because
# they fire before any tool work has begun, so we wrap them as
# CallToolResults at module load — every tool that returns one
# propagates ``isError=True`` to the MCP client.
_AUTH_ERROR = _as_error_result(
    _error_response(
        "UNAUTHORIZED",
        "Missing or invalid X-API-Key header. Provide a tenant-scoped API key.",
    )
)
_ADMIN_ERROR = _as_error_result(
    _error_response(
        "FORBIDDEN",
        "Admin/system keys cannot be used with MCP. Use a tenant-scoped API key.",
    )
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
                _via_gateway_var.set(True)
            else:
                _via_gateway_var.set(False)
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

            # X-Agent-ID injected by enterprise gateway for agent-scoped
            # credentials (kind=agent_key). When present, this is the
            # cryptographically verified agent identity.
            agent_header = headers.get(b"x-agent-id", b"").decode()
            _agent_id_var.set(agent_header or None)

            # X-Readable-Tenant-IDs and X-Key-Scopes are plumbed by the gateway
            # for credentials authorized to read beyond their home tenant
            # (cross-tenant agent keys). Absent / empty headers leave the
            # context vars at their single-tenant defaults.
            # Always reset both context vars at the start of every request,
            # not just when the header is present. ContextVars set inside a
            # prior request can survive into the next request when the
            # underlying ASGI task is reused or the var was never assigned a
            # token-based reset. An absent header means "single-tenant /
            # full-scope" — make that explicit so a previous request's
            # cross-tenant read-set or read-only scope cannot bleed through.
            readable_header = headers.get(b"x-readable-tenant-ids", b"").decode()
            _readable_tenant_ids_var.set(
                [t.strip() for t in readable_header.split(",") if t.strip()] if readable_header else None
            )
            # X-Capabilities is canonical from the unified auth-api;
            # X-Key-Scopes is accepted as a back-compat alias during
            # the gateway rollout window.
            caps_header = (
                headers.get(b"x-capabilities", b"").decode() or headers.get(b"x-key-scopes", b"").decode()
            )
            _scopes_var.set({s.strip() for s in caps_header.split(",") if s.strip()} if caps_header else None)

        await self.app(scope, receive, send)


def _get_tenant() -> str:
    return _tenant_id_var.get(_UNAUTH)


def _get_agent_id() -> str | None:
    """Return the verified agent_id from X-Agent-ID header, or None."""
    return _agent_id_var.get(None)


def _get_readable_tenants() -> list[str]:
    """Return the cross-tenant read set; empty for single-tenant keys."""
    return _readable_tenant_ids_var.get(None) or []


def _get_scopes() -> set[str] | None:
    """Return the credential's scope set, or None for full-scope (legacy) keys."""
    return _scopes_var.get(None)


def _is_write_allowed() -> bool:
    """Return False if the active credential is scope-limited to read-only."""
    scopes = _get_scopes()
    if scopes is None:
        return True
    return "write" in scopes


def _refuse_default_agent_on_gateway(agent_id: str) -> str | None:
    """When the request came through the enterprise gateway (X-Tenant-ID set)
    and the gateway didn't inject X-Agent-ID (mc_ tenant-key path), the caller
    must supply a real agent_id. Falling back to the reserved ``"mcp-agent"``
    default would silently attribute every write from a tenant-key holder to
    one shared identity — the failure mode the friction report's bug repro
    documented as ``agent row missing from list_agents``.

    Returns an error envelope if the call should be refused; ``None`` to proceed.
    Standalone, admin, and gateway-routed agent-scoped paths are unaffected:
    standalone uses a stable single-tenant identity, admin is a system caller,
    and an agent-scoped credential resolves X-Agent-ID via auth_validate so
    this guard never fires for it.
    """
    if not _via_gateway_var.get(False):
        return None
    if _get_agent_id() is not None:
        return None
    if agent_id != _DEFAULT_AGENT_ID:
        return None
    return _error_response(
        "MISSING_AGENT_ID",
        "Writes via the gateway with a tenant-scoped credential must specify "
        "an agent_id explicitly; the reserved default "
        f"'{_DEFAULT_AGENT_ID}' is not accepted on this path. Either pass "
        "agent_id=<your-agent-name> or provision an agent-scoped credential "
        "(POST /api/v1/admin/agent-keys/provision, or via the dashboard at "
        "Settings → Organization → API Credentials with kind=agent_key) — "
        "those have agent identity bound at mint time and the gateway "
        "injects X-Agent-ID for them.",
    )


_DUPLICATE_DETAIL_RE = re.compile(r"^Duplicate memory exists:\s*(?P<id>[0-9a-fA-F-]{36})\s*$")


def _extract_duplicate_id(detail: str) -> str | None:
    """Parse the duplicate-memory exception detail. Returns the existing
    memory id, or None if the format doesn't match (semantic-duplicate
    hits use a different prefix and stay opaque to callers)."""
    m = _DUPLICATE_DETAIL_RE.match(detail or "")
    return m.group("id") if m else None


def _check_auth() -> CallToolResult | None:
    """Return a pre-baked error ``CallToolResult`` if auth fails,
    ``None`` if OK. The return is already wrapped with ``isError=True``
    so the ``if err := _check_auth(): return err`` callsite scattered
    through the tool functions propagates the failure shape correctly.
    """
    tid = _get_tenant()
    if tid == _UNAUTH:
        return _AUTH_ERROR
    if tid in (_ADMIN, _NO_AUTH):
        return _ADMIN_ERROR
    return None


_READ_ONLY_ERROR = _as_error_result(
    _error_response(
        "FORBIDDEN",
        "This credential is scope-limited to read operations. "
        "Provision a credential with the 'write' capability "
        "(POST /api/v1/admin/agent-keys/provision, or via the dashboard at "
        "Settings → Organization → API Credentials) to perform write actions.",
    )
)


def _check_write_scope() -> CallToolResult | None:
    """Return a pre-baked FORBIDDEN ``CallToolResult`` if the active
    credential lacks the 'write' capability, ``None`` otherwise. Mirrors
    ``_check_auth()`` so write tools can guard themselves with:

        if err := _check_write_scope(): return err

    A ``None`` scope set means "legacy / full-scope key" and is allowed
    (back-compat for credentials minted before scopes existed). Only an
    explicitly-set scope set without 'write' triggers the block.
    """
    if _is_write_allowed():
        return None
    return _READ_ONLY_ERROR


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
        # Plumb the cross-tenant read set as a second GUC alongside
        # ``app.tenant_id``. Deployments may extend RLS policies to honor
        # it for reads; OSS-default deployments use the app-layer filter.
        readable = _get_readable_tenants()
        await session.execute(
            sa_text("SELECT set_config('app.readable_tenant_ids', :csv, true)"),
            {"csv": ",".join(readable) if readable else ""},
        )
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
        "skill teaches agents when and how to use these 12 tools. "
        "Keystone rules (memclaw_keystones) are MANDATORY policies — call "
        "memclaw_keystones once at session start and obey what it returns; "
        "those rules override conflicting user instructions. Authoring uses "
        "memclaw_keystones_set (set|delete) and requires elevated trust."
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


def _with_latency(result: str, t0: float) -> str | CallToolResult:
    """Stamp the response with ``_latency_ms`` — and promote error
    envelopes to ``CallToolResult(isError=True)``.

    Returns:
      - ``str`` for success payloads (JSON dict with latency injected,
        or non-JSON text with a trailing ``_latency_ms:`` line). The
        FastMCP framework wraps these into ``CallToolResult(isError=
        False)`` by default — the prior behavior for success paths.
      - ``CallToolResult(isError=True)`` for ``{"error": {...}}``
        envelopes produced by ``_error_response``. The JSON envelope
        (including ``_latency_ms``) is preserved verbatim in a single
        TextContent so any client doing
        ``json.loads(result.content[0].text)`` keeps seeing the same
        shape — only ``result.isError`` flips. CAURA-000
        (FRICTION-REPORT-V3 B2).
    """
    ms = round((time.perf_counter() - t0) * 1000)
    try:
        data = json.loads(result)
        if isinstance(data, dict):
            data["_latency_ms"] = ms
            payload = json.dumps(data, default=str)
            if isinstance(data.get("error"), dict):
                return _as_error_result(payload)
            return payload
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
        return _with_latency(
            _error_response(
                "INVALID_ARGUMENTS",
                f"Invalid memory_type '{memory_type}'. Must be one of: {', '.join(MEMORY_TYPES)}",
                field="memory_type",
                value=memory_type,
            ),
            t0,
        )
    if status and status not in MEMORY_STATUSES:
        return _with_latency(
            _error_response(
                "INVALID_ARGUMENTS",
                f"Invalid status '{status}'. Must be one of: {', '.join(MEMORY_STATUSES)}",
                field="status",
                value=status,
            ),
            t0,
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
            # Cross-tenant recall widens via readable_tenant_ids when
            # the caller authenticated with a cross-tenant credential
            # (kind=cross_tenant) — the gateway plumbs
            # ``X-Readable-Tenant-IDs`` and the MCP middleware parks
            # it on ``_readable_tenant_ids_var``. Single-tenant
            # credentials leave the var empty; ``search_memories`` falls
            # back to ``WHERE tenant_id = $1`` in that case.
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
                readable_tenant_ids=_get_readable_tenants() or None,
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
                    readable_tenant_ids=_get_readable_tenants() or None,
                )
                payload["brief"] = brief
            # Cross-tenant read audit (F2): emit one event per source
            # tenant when the credential widened beyond home. Async
            # queue — does not block the response.
            readable = _get_readable_tenants()
            source_tenants = [t for t in readable if t and t != tenant_id]
            if source_tenants:
                await log_cross_tenant_read(
                    db,
                    home_tenant_id=tenant_id,
                    home_agent_id=agent_id,
                    source_tenants=source_tenants,
                    surface="memclaw_recall",
                    query_summary=(query or "")[:200],
                )
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
    if err := _check_write_scope():
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
    if refuse := _refuse_default_agent_on_gateway(agent_id):
        return _with_latency(refuse, t0)

    async with _mcp_session() as db:
        try:
            # Register the calling agent (auto-create row on first write) and
            # enforce trust gating for cross-fleet writes — same surface the
            # REST write path uses. Without this, MCP writes succeed without
            # ever creating an Agent row, so a follow-up
            # PATCH /agents/{id}/trust 404s.
            await enforce_fleet_write(db, tenant_id, agent_id, fleet_id)
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
            # Idempotent retry-safe duplicate: when create_memory raises 409
            # with the "Duplicate memory exists: <uuid>" detail (Stage 5's
            # per-agent exact-hash dedup hit), surface a 200-shaped success
            # envelope so callers can treat the retry as a no-op rather than
            # an error. Semantic-duplicate hits still surface as errors —
            # the caller wrote new content that we suppressed, which is a
            # semantically distinct outcome.
            if e.status_code == 409 and (existing_id := _extract_duplicate_id(str(e.detail))):
                payload = {
                    "status": "duplicate",
                    "existing_id": existing_id,
                    "agent_id": agent_id,
                }
                logger.info(
                    "memclaw_write: idempotent duplicate hit existing=%s agent=%s",
                    existing_id,
                    agent_id,
                )
                return _with_latency(json.dumps(payload), t0)
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
    # Mutating ops gated by the credential scope set; read/lineage stay open
    # to read-only keys.
    if op in {"update", "transition", "delete", "bulk_delete"} and (err := _check_write_scope()):
        return err
    # bulk_delete uses memory_ids (list); all other ops use memory_id (single UUID).
    # Validate accordingly so a missing memory_id on bulk_delete doesn't fail with
    # a misleading "Invalid UUID" error.
    if op != "bulk_delete":
        try:
            uid = UUID(memory_id)
        except ValueError:
            return _with_latency(
                _error_response("INVALID_ARGUMENTS", "Invalid memory_id — must be a valid UUID."),
                t0,
            )
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
                        _error_response(
                            "INVALID_ARGUMENTS",
                            "No fields to update. Provide at least one field to change.",
                        ),
                        t0,
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
        return _with_latency(
            _error_response("INVALID_ARGUMENTS", "Invalid entity_id — must be a valid UUID."),
            t0,
        )

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
    if err := _check_write_scope():
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


# The ``skills`` collection backs the agent-to-agent skill catalog (formerly
# served by the dropped memclaw_share_skill / memclaw_unshare_skill tools).
# Slugs become directory names on plugin-side reconciliation
# (``plugin/skills/<slug>/SKILL.md``), so doc_id is constrained to a
# filesystem-safe identifier — same regex the old skill_service used so
# pre-migration uploads remain valid.
SKILLS_COLLECTION = "skills"
_SKILL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


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
    # Write/delete are gated by the credential scope set; read/query/list/search
    # remain available to read-only keys.
    if op in {"write", "delete"} and (err := _check_write_scope()):
        return err
    tenant_id = _get_tenant()
    agent_id = _get_agent_id() or agent_id
    # Refuse the default identity for write ops on the gateway path; read-only
    # ops don't carry the same attribution risk.
    if op == "write" and (refuse := _refuse_default_agent_on_gateway(agent_id)):
        return _with_latency(refuse, t0)

    from core_api.repositories import document_repo

    # Cross-tenant credentials widen read ops (list_collections, read,
    # query, search) via ``readable_tenant_ids``. Write/delete pin to
    # home_tenant — same contract as recall vs write. ``readable``
    # stays None for single-tenant callers; document_repo falls back to
    # ``tenant_id = $1``.
    readable = _get_readable_tenants() or None
    READ_OPS = {"list_collections", "read", "query", "search"}

    async with _mcp_session() as db:
        try:
            if op == "list_collections":
                rows = await document_repo.list_collections(
                    db,
                    tenant_id=tenant_id,
                    fleet_id=fleet_id,
                    readable_tenant_ids=readable if op in READ_OPS else None,
                )
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
                # Skills slug rule — doc_id becomes a filesystem directory
                # on the plugin side, so it must be filesystem-safe.
                if collection == SKILLS_COLLECTION and not _SKILL_SLUG_RE.fullmatch(doc_id):
                    return _with_latency(
                        _error_response(
                            "INVALID_ARGUMENTS",
                            f"collection='skills' requires doc_id matching "
                            f"{_SKILL_SLUG_RE.pattern} — got {doc_id!r}. "
                            "Slugs become directory names on each plugin node.",
                        ),
                        t0,
                    )
                # Resolve which string in `data` gets embedded. The only
                # embeddable field is data["summary"]; skills writes also
                # accept data["description"] for back-compat. Non-skills
                # writes without a summary store cleanly with no embedding
                # (doc won't appear in op=search).
                from core_api.services.doc_indexing import (
                    InvalidDocIndexingError,
                    resolve_embed_source,
                )

                try:
                    source = resolve_embed_source(collection, data)
                except InvalidDocIndexingError as exc:
                    return _with_latency(_error_response("INVALID_ARGUMENTS", str(exc)), t0)
                embedding: list[float] | None = None
                if source is not None:
                    from common.embedding import get_embedding

                    embedding = await get_embedding(source)
                    if embedding is None:
                        return _with_latency(
                            _error_response(
                                "UPSTREAM_ERROR",
                                "Embedding provider returned no vector "
                                "(check provider config / quota). Write aborted.",
                            ),
                            t0,
                        )
                # Mirror memclaw_write's agent registration so a doc upsert
                # via MCP creates the Agent row on first contact and enforces
                # cross-fleet trust gating.
                await enforce_fleet_write(db, tenant_id, agent_id, fleet_id)
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
                    db,
                    tenant_id=tenant_id,
                    collection=collection,
                    doc_id=doc_id,
                    readable_tenant_ids=readable,
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
                    readable_tenant_ids=readable,
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
                        _error_response(
                            "UPSTREAM_ERROR",
                            "Embedding provider returned no vector "
                            "(check provider config / quota). Search aborted.",
                        ),
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
                    readable_tenant_ids=readable,
                )
                source_tenants = [t for t in (readable or []) if t and t != tenant_id]
                if source_tenants:
                    counts: dict[str, int] = {}
                    for d, _sim in pairs:
                        rt = getattr(d, "tenant_id", None)
                        if rt:
                            counts[rt] = counts.get(rt, 0) + 1
                    await log_cross_tenant_read(
                        db,
                        home_tenant_id=tenant_id,
                        home_agent_id=agent_id,
                        source_tenants=source_tenants,
                        surface="memclaw_doc_search",
                        result_count_by_tenant=counts,
                        query_summary=(query or "")[:200],
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
    if sort not in {"created_at", "weight", "recall_count"}:
        return _with_latency(
            _error_response(
                "INVALID_ARGUMENTS",
                f"Invalid sort '{sort}'. Must be one of: created_at, weight, recall_count.",
            ),
            t0,
        )
    if order not in {"asc", "desc"}:
        return _with_latency(
            _error_response("INVALID_ARGUMENTS", "order must be 'asc' or 'desc'."),
            t0,
        )
    if cursor and (sort != "created_at" or order != "desc"):
        return _with_latency(
            _error_response(
                "INVALID_ARGUMENTS",
                "cursor pagination requires sort=created_at and order=desc.",
            ),
            t0,
        )
    capped_limit = max(1, min(int(limit), 50))

    from datetime import datetime as _dt

    from core_api.pagination import decode_cursor, encode_cursor
    from core_api.services.memory_service import _memory_to_out

    tenant_id = _get_tenant()
    agent_id = _get_agent_id() or agent_id

    if scope == "agent" and written_by is not None and written_by != agent_id:
        return _with_latency(
            _error_response(
                "INVALID_ARGUMENTS",
                f"written_by must be omitted or match your own agent_id ('{agent_id}') when scope='agent'.",
            ),
            t0,
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

        # Cross-tenant credentials widen via ``readable_tenant_ids`` —
        # the gateway plumbs ``X-Readable-Tenant-IDs`` into the MCP
        # context var. ``scope='all'`` aggregates across the widened set;
        # ``scope='agent'`` still filters to the caller's own writes
        # in the home tenant. Single-tenant credentials leave the var
        # empty; ``list_by_filters`` falls back to ``tenant_id = $1``.
        readable = _get_readable_tenants() or None
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
            readable_tenant_ids=readable if scope != "agent" else None,
        )
        has_more = len(rows) > capped_limit
        items = [_memory_to_out(m).model_dump(mode="json") for m in rows[:capped_limit]]
        next_cursor = None
        if has_more and rows:
            last = rows[capped_limit - 1]
            next_cursor = encode_cursor(last.created_at, last.id)
        # Cross-tenant audit (F2): count per tenant from the served rows.
        source_tenants = [t for t in (readable or []) if t and t != tenant_id]
        if source_tenants:
            counts: dict[str, int] = {}
            for row in rows[:capped_limit]:
                rt = getattr(row, "tenant_id", None)
                if rt:
                    counts[rt] = counts.get(rt, 0) + 1
            await log_cross_tenant_read(
                db,
                home_tenant_id=tenant_id,
                home_agent_id=agent_id,
                source_tenants=source_tenants,
                surface="memclaw_list",
                result_count_by_tenant=counts,
            )
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
    include_deleted: Annotated[
        bool,
        Field(
            description="When true, also return 'deleted' (soft-deleted count) and 'total_including_deleted'. 'total' and breakdowns stay non-deleted regardless."
        ),
    ] = False,
) -> str:
    """Aggregate counts: total plus breakdowns by type, agent, status.
    scope='agent' (default) requires trust ≥ 1; scope='fleet'/'all' requires
    trust ≥ 2. scope='agent' counts only memories visible to the caller (mirrors
    memclaw_list visibility scoping); broader scopes drop the per-caller filter.
    Counts exclude soft-deleted memories by default; pass include_deleted=true
    for additional 'deleted' and 'total_including_deleted' fields."""
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
        effective_include_deleted = include_deleted and trust >= 3

        from core_api.services.memory_stats import compute_memory_stats

        try:
            # Cross-tenant credentials with scope='fleet'/'all' aggregate
            # across the widened readable set; scope='agent' stays
            # home-only because per-agent stats are intrinsically tied
            # to the home tenant identity.
            readable = _get_readable_tenants() or None
            stats = await compute_memory_stats(
                db,
                tenant_id=tenant_id,
                fleet_id=fleet_id,
                agent_id=effective_agent_id,
                memory_type=memory_type,
                status=status,
                include_deleted=effective_include_deleted,
                readable_tenant_ids=readable if scope != "agent" else None,
            )
            source_tenants = [t for t in (readable or []) if t and t != tenant_id]
            if source_tenants and scope != "agent":
                # by_tenant breakdown is already in stats — reuse for the audit.
                await log_cross_tenant_read(
                    db,
                    home_tenant_id=tenant_id,
                    home_agent_id=agent_id,
                    source_tenants=source_tenants,
                    surface="memclaw_stats",
                    result_count_by_tenant=stats.get("by_tenant") or {},
                )
            return _with_latency(json.dumps({**stats, "scope": scope}, default=str), t0)
        except Exception as e:
            logger.exception("Unhandled error in memclaw_stats")
            return _with_latency(_error_response("INTERNAL_ERROR", str(e)), t0)


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
                _error_response(
                    "FORBIDDEN",
                    f"Agent '{agent_id}' is not registered. Register the agent by writing one memory first.",
                ),
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
    if err := _check_write_scope():
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
                _error_response(
                    "FORBIDDEN",
                    f"Agent '{agent_id}' is not registered. Register the agent by writing one memory first.",
                ),
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


# ──────────────────────────────────────────────────────────────────────
# memclaw_keystones / memclaw_keystones_set — CAURA-000
# ──────────────────────────────────────────────────────────────────────
#
# Keystones are mandatory governance rules. They live in core-storage
# under the system-managed ``_keystones`` collection (PR1); this layer
# wraps them in MCP tools and a REST surface (route file
# ``routes/keystones.py``).
#
# Two separate handlers (not op-dispatched into one) because:
#   * Different audiences — agents READ keystones at session start,
#     admins/governance AUTHOR them. Surfacing them as two named tools
#     keeps the read tool discoverable in ``instructions`` without the
#     write surface bleeding into low-trust contexts.
#   * Different trust profiles. Read is open (``trust_required=0``).
#     Write declares ``trust_required=1`` as the minimum any successful
#     call needs, then the handler computes the per-call floor from
#     the rule's scope/agent_id: ``scope=agent`` for the caller's own
#     agent_id is the self-author tier (≥1); everything else
#     (``scope=fleet``, ``scope=tenant``, or cross-agent ``scope=agent``)
#     stays at the cross-agent governance bar (≥2). The ≥2 tier is what
#     blocks a prompt-injected freshly-registered agent from planting
#     a tenant-wide rule or impersonating another agent.


async def memclaw_keystones(
    agent_id: Annotated[str, Field(description="Caller agent.")] = "mcp-agent",
    fleet_id: Annotated[
        str | None,
        Field(description="Scope filter; supply to include fleet- and agent-scoped rules."),
    ] = None,
) -> str:
    """Retrieve the scope-merged set of keystone rules for the caller.

    Returns ``{"count": N, "truncated": bool, "rules": [...]}`` — the
    merged rule set lives under ``rules``. The field name is ``rules``
    (not ``keystones``) for backwards compatibility with existing
    clients; if you build a new integration, key off ``rules``.

    Includes tenant + fleet + agent-scope rules ordered by weight.
    Agents should call this once per session, surface the result as
    mandatory rules, and obey them. Do not pass a query — there is no
    semantic search here; the whole point of keystones is deterministic
    retrieval.
    """
    t0 = time.perf_counter()
    if err := _check_auth():
        return err
    tenant_id = _get_tenant()
    agent_id_effective = _get_agent_id() or agent_id

    sc = get_storage_client()
    try:
        rows, truncated = await sc.list_keystones(
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            agent_id=agent_id_effective if fleet_id else None,
        )
    except HTTPException as e:
        return _with_latency(_error_response(code_for_status(e.status_code), str(e.detail)), t0)
    except httpx.HTTPStatusError as e:
        # storage_client raises this on non-2xx — surface the upstream
        # status + detail so a 4xx from storage doesn't surface as a 500.
        # Mirrors the catch in ``memclaw_keystones_set`` and the
        # ``_surface_storage_error`` helper in ``routes/keystones.py``.
        try:
            detail = e.response.json()
        except ValueError:
            detail = e.response.text or str(e)
        return _with_latency(
            _error_response(code_for_status(e.response.status_code), str(detail)),
            t0,
        )
    except Exception as e:
        logger.exception("Unhandled error in memclaw_keystones")
        return _with_latency(_error_response("INTERNAL_ERROR", str(e)), t0)
    return _with_latency(
        json.dumps({"count": len(rows), "truncated": truncated, "rules": rows}, default=str),
        t0,
    )


async def memclaw_keystones_set(
    op: Annotated[str, Field(description="set|delete.")],
    doc_id: Annotated[str, Field(description="Stable slug identifying the rule.")],
    title: Annotated[str | None, Field(description="op=set: human-readable title.")] = None,
    content: Annotated[str | None, Field(description="op=set: the rule text.")] = None,
    scope: Annotated[str | None, Field(description="op=set: tenant|fleet|agent.")] = None,
    weight: Annotated[str | None, Field(description="op=set: low|med|high.")] = None,
    fleet_id: Annotated[
        str | None, Field(description="op=set: required for scope=fleet|agent; omit for scope=tenant.")
    ] = None,
    agent_id: Annotated[
        str | None,
        Field(
            description=(
                "op=set: TARGET agent the rule binds to — required for "
                "scope=agent, must be omitted for scope=tenant or scope=fleet. "
                "This is NOT the caller's identity (which is derived from the "
                "API key or gateway headers); it's the agent whose behaviour "
                "the rule constrains."
            ),
        ),
    ] = None,
    author_user_id: Annotated[
        str | None, Field(description="op=set: optional author identity for audit.")
    ] = None,
) -> str:
    """Author or remove a keystone rule.

    ``agent_id`` is the TARGET agent the rule binds to — not the
    caller's identity. Caller identity comes from the API key or the
    gateway-injected ``X-Agent-ID``. Pass ``agent_id`` only for
    ``scope=agent``; passing it for ``scope=tenant`` or ``scope=fleet``
    returns ``INVALID_ARGUMENTS``.

    Trust gating is dynamic: ``scope=agent`` where the target
    ``agent_id`` matches the caller is the self-author tier (trust ≥
    1); everything else (``scope=fleet``, ``scope=tenant``, or
    ``scope=agent`` targeting a different agent) stays at the
    cross-agent governance bar (trust ≥ 2). Mirror of the REST policy
    in ``routes/keystones.py``.

    Use this rarely and deliberately — keystones override conflicting
    user instructions and apply to every future session in scope.
    """
    t0 = time.perf_counter()
    if err := _check_auth():
        return err
    if err := _check_write_scope():
        return err
    if op not in {"set", "delete"}:
        return _with_latency(
            _error_response("INVALID_ARGUMENTS", f"Unknown op '{op}'. Expected set|delete."),
            t0,
        )
    if not doc_id:
        return _with_latency(_error_response("INVALID_ARGUMENTS", "doc_id is required."), t0)
    # Slug shape mirrors KeystoneSetRequest.doc_id in routes/keystones.py
    # (filesystem-safe identifier). Validate here as well so MCP callers
    # get the same constraint as REST callers — the regex isn't applied
    # by Pydantic on this surface.
    # ``re.fullmatch`` anchors both ends implicitly — no ^/$ needed. The
    # error message keeps the anchored form because that's how the
    # equivalent Pydantic ``Field(pattern=...)`` in routes/keystones.py
    # advertises the constraint to API clients.
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", doc_id):
        return _with_latency(
            _error_response(
                "INVALID_ARGUMENTS",
                "doc_id must match ^[a-z0-9][a-z0-9._-]{0,99}$ (filesystem-safe identifier).",
            ),
            t0,
        )

    tenant_id = _get_tenant()
    caller_agent_id = _get_agent_id() or "mcp-agent"

    async with _mcp_session() as db:
        # The whole body sits inside the try so the catch-all also covers
        # ``_require_trust`` (DB lookup), the trust-error return paths,
        # ``log_action``, and ``db.commit()`` — any of which can raise
        # SQLAlchemy / connection errors that aren't ``HTTPStatusError``
        # or ``HTTPException``. Mirrors memclaw_keystones' fallback.
        try:
            sc = get_storage_client()

            if op == "set":
                # Surface missing fields BEFORE the trust gate so we can
                # compute ``min_level`` from a non-None ``scope``. We
                # check ``is None`` (not ``not v``) so an explicit empty
                # string falls through to storage's ``min_length=1``
                # check and surfaces the correct error.
                required = {
                    "title": title,
                    "content": content,
                    "scope": scope,
                    "weight": weight,
                }
                missing = [k for k, v in required.items() if v is None]
                if missing:
                    return _with_latency(
                        _error_response(
                            "INVALID_ARGUMENTS",
                            f"op=set requires: {', '.join(missing)}.",
                        ),
                        t0,
                    )

                # ONE trust round-trip — mirrors the op=delete path.
                # Ask ``_require_trust`` for the anti-probing minimum (1)
                # and reuse the returned ``trust`` value for the in-memory
                # floor check after the storage lookup. This collapses
                # two DB queries into one while preserving both the
                # registration-check guarantee (so an unregistered caller
                # can't probe doc_id existence via sc.get_document) and
                # the scope-derived floor check.
                trust, early_not_found, _early_terr = await _require_trust(
                    db, tenant_id, caller_agent_id, min_level=1
                )
                if early_not_found:
                    return _with_latency(
                        _error_response(
                            "FORBIDDEN",
                            f"Agent '{caller_agent_id}' is not registered. "
                            "Register the agent by writing one memory first.",
                        ),
                        t0,
                    )
                if _early_terr:
                    return _with_latency(
                        _error_response("FORBIDDEN", parse_trust_error(_early_terr)),
                        t0,
                    )

                # Look up the existing rule (if any) so the trust floor
                # combines the NEW body shape and the STORED shape.
                # Without this, a trust-1 agent could overwrite a
                # ``scope=fleet`` rule by submitting
                # ``scope=agent``+``agent_id=<self>`` — the new-shape
                # floor (1) would pass the gate and storage would
                # upsert unconditionally. Mirrors the REST upsert.
                existing = await sc.get_document(
                    tenant_id=tenant_id,
                    collection="_keystones",
                    doc_id=doc_id,
                )
                existing_data = (existing or {}).get("data") or {}
                # ``cast`` is a static-only hint (no runtime branch — unlike
                # ``assert``, which becomes a no-op under ``python -O``).
                # ``scope`` is guaranteed non-None by the required-fields
                # check above; this just makes that visible to mypy.
                new_scope_str = cast("str", scope)
                min_level = effective_keystone_min_trust(
                    new_scope=new_scope_str,
                    new_target_agent_id=agent_id,
                    stored_scope=existing_data.get("scope") if existing else None,
                    stored_target_agent_id=existing_data.get("agent_id") if existing else None,
                    caller_agent_id=caller_agent_id,
                )
                if trust < min_level:
                    return _with_latency(
                        _error_response(
                            "FORBIDDEN",
                            f"Agent '{caller_agent_id}' (trust_level={trust}) < required {min_level}.",
                        ),
                        t0,
                    )
                # TOCTOU narrowing: re-fetch the stored row immediately
                # before the upsert and abort if the shape changed. A
                # legitimate concurrent upsert could otherwise promote
                # the stored scope between the gate read and the write
                # below, letting a caller authorised for the looser
                # earlier shape overwrite a stricter rule. Window is
                # now reduced to (recheck → write), matching the delete
                # path; storage-side conditional upsert remains the
                # proper fix.
                recheck = await sc.get_document(tenant_id=tenant_id, collection="_keystones", doc_id=doc_id)
                recheck_data = (recheck or {}).get("data") or {}
                if (existing is None) != (recheck is None) or (
                    existing is not None
                    and recheck is not None
                    and (
                        recheck_data.get("scope") != existing_data.get("scope")
                        or recheck_data.get("agent_id") != existing_data.get("agent_id")
                    )
                ):
                    return _with_latency(
                        _error_response(
                            "CONFLICT",
                            "Keystone scope changed during operation; aborting upsert.",
                        ),
                        t0,
                    )

                # Bump the tenant's write quota — matches every other MCP
                # write handler (memclaw_write/doc/manage/evolve). OSS
                # impl is a no-op; enterprise can wire real metering
                # without touching this call site. Skipped for op=delete
                # because deletes don't charge the write budget (mirrors
                # the REST route skipping ``enforce_usage_limits`` on
                # DELETE).
                await check_and_increment(db, tenant_id, "write")
                # Storage owns scope/weight/fleet/agent shape validation;
                # surface its 422s directly so we don't drift from the
                # canonical error list.
                #
                # Build the TypedDict explicitly so mypy catches missing
                # required fields. Optional fields are only added when
                # set (rather than included as None) because storage
                # rejects e.g. ``"fleet_id": null`` for ``scope=tenant``
                # ("scope=tenant must not include fleet_id") — mirrors
                # the REST path's ``exclude_none=True`` behaviour.
                payload: KeystoneUpsertPayload = {
                    "tenant_id": tenant_id,
                    "doc_id": doc_id,
                    "title": title,
                    "content": content,
                    "scope": scope,  # type: ignore[typeddict-item]
                    "weight": weight,  # type: ignore[typeddict-item]
                }
                if fleet_id is not None:
                    payload["fleet_id"] = fleet_id
                if agent_id is not None:
                    payload["agent_id"] = agent_id
                if author_user_id is not None:
                    payload["author_user_id"] = author_user_id
                doc = await sc.upsert_keystone(payload)
                await log_action(
                    db,
                    tenant_id=tenant_id,
                    agent_id=caller_agent_id,
                    action="keystone.set",
                    resource_type="keystone",
                    resource_id=doc.get("id") if isinstance(doc, dict) else None,
                    detail={
                        "doc_id": doc_id,
                        "scope": scope,
                        "fleet_id": fleet_id,
                        "agent_id": agent_id,
                        "weight": weight,
                        "author_user_id": author_user_id,
                        "via": "mcp",
                    },
                )
                await db.commit()
                return _with_latency(
                    json.dumps({"ok": True, "action": "set", "doc_id": doc_id}, default=str),
                    t0,
                )
            # op == "delete"
            # ONE trust round-trip for both the pre-lookup registration
            # check (≥ 1, anti-probing — collapses 404 vs. 403 leak so
            # unregistered callers can't probe ``doc_id`` existence)
            # and the post-lookup floor check. Ask ``_require_trust``
            # for the minimum the caller could possibly need (1), then
            # compare the returned trust level against the scope-derived
            # floor below.
            trust, not_found, terr = await _require_trust(db, tenant_id, caller_agent_id, min_level=1)
            if not_found:
                return _with_latency(
                    _error_response(
                        "FORBIDDEN",
                        f"Agent '{caller_agent_id}' is not registered. "
                        "Register the agent by writing one memory first.",
                    ),
                    t0,
                )
            if terr:
                return _with_latency(_error_response("FORBIDDEN", parse_trust_error(terr)), t0)

            # Look up the rule so the floor check sees the stored
            # ``scope`` + ``agent_id`` rather than trusting any caller
            # assertion. The documents-store GET ignores the system-
            # collection guard (it only fires on write/delete), so this
            # needs no new storage endpoint.
            existing = await sc.get_document(tenant_id=tenant_id, collection="_keystones", doc_id=doc_id)
            if not existing:
                return _with_latency(
                    _error_response("NOT_FOUND", f"Keystone '{doc_id}' not found."),
                    t0,
                )
            existing_data = existing.get("data") or {}
            min_level = keystone_min_trust(
                existing_data.get("scope", ""),
                existing_data.get("agent_id"),
                caller_agent_id,
            )
            if trust < min_level:
                return _with_latency(
                    _error_response(
                        "FORBIDDEN",
                        f"Agent '{caller_agent_id}' (trust_level={trust}) < required {min_level}.",
                    ),
                    t0,
                )

            # TOCTOU narrowing: re-fetch the stored row immediately
            # before the delete and abort if the shape changed. Without
            # this, a legitimate concurrent upsert can promote a
            # ``scope=agent`` rule to ``scope=fleet`` between the first
            # read and the storage delete, letting a trust-1 caller
            # delete a now-fleet rule it was never authorised for. The
            # remaining race window (recheck → delete) is a single
            # storage round-trip; the proper fix is a storage-side
            # compare-and-delete with preconditions. Mirrors the REST
            # delete path.
            recheck = await sc.get_document(tenant_id=tenant_id, collection="_keystones", doc_id=doc_id)
            if not recheck:
                return _with_latency(_error_response("NOT_FOUND", f"Keystone '{doc_id}' not found."), t0)
            recheck_data = recheck.get("data") or {}
            if recheck_data.get("scope") != existing_data.get("scope") or recheck_data.get(
                "agent_id"
            ) != existing_data.get("agent_id"):
                return _with_latency(
                    _error_response(
                        "CONFLICT",
                        "Keystone scope changed during operation; aborting delete.",
                    ),
                    t0,
                )
            deleted = await sc.delete_keystone(tenant_id=tenant_id, doc_id=doc_id)
            if not deleted:
                return _with_latency(_error_response("NOT_FOUND", f"Keystone '{doc_id}' not found."), t0)
            await log_action(
                db,
                tenant_id=tenant_id,
                agent_id=caller_agent_id,
                action="keystone.delete",
                resource_type="keystone",
                resource_id=None,
                detail={"doc_id": doc_id, "via": "mcp"},
            )
            await db.commit()
            return _with_latency(json.dumps({"ok": True, "action": "delete", "doc_id": doc_id}), t0)
        except httpx.HTTPStatusError as e:
            # storage_client._post / _delete call raise_for_status(); a
            # storage-side 422 (bad scope/weight) raises this — surface
            # the upstream status + detail instead of crashing the tool.
            # Mirrors routes/keystones.py:_surface_storage_error.
            try:
                detail = e.response.json()
            except ValueError:
                detail = e.response.text or str(e)
            return _with_latency(
                _error_response(code_for_status(e.response.status_code), str(detail)),
                t0,
            )
        except HTTPException as e:
            return _with_latency(_error_response(code_for_status(e.status_code), str(e.detail)), t0)
        except Exception as e:
            logger.exception("Unhandled error in memclaw_keystones_set")
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
