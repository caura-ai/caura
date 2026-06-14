"""Request-observation middleware — per-endpoint API usage metrics.

Pure ASGI middleware — matches the ``SecurityHeadersMiddleware`` /
``RequestTimeoutMiddleware`` / ``IngestBodySizeMiddleware`` pattern rather
than ``BaseHTTPMiddleware`` (which wraps in Starlette's task groups and has
known edge cases around cancellation and streaming responses).

Emits exactly one structured ``http.request`` log event per HTTP request,
carrying the *templated* route, method, status, and wall-clock duration.
These events back a log-based distribution metric for per-endpoint
API-usage dashboards on Cloud-Logging deployments; on file-logging
deployments they land in the JSON log file just like any other log line.

Cardinality contract
--------------------
``http_route`` is ALWAYS the route template read from
``scope["route"].path`` (e.g. ``/api/v1/memories/{id}``) or the literal
``"unmatched"`` when no route matched (404s, or failures before routing).
It is NEVER the raw request path with path params interpolated — raw paths
would explode the metric's label cardinality and make it expensive.

``scope["route"]`` only exists AFTER the router has run, so this middleware
must inspect it once the downstream app returns, not before.

Timing
------
Duration is measured around the full downstream call. For streaming / SSE /
long-poll routes that means the value reflects the entire request lifetime
(can be 60s+); the resulting latency distribution is expected to be bimodal.

Logging
-------
Uses stdlib ``logging`` with ``extra={...}`` rather than a structlog-native
logger: every other call site in core-api logs this way, and
``common.structlog_config`` wires a stdlib bridge that promotes ``extra``
keys to top-level fields in the GCP JSON payload (pinned by
``tests/test_logging.py::test_stdlib_logger_extras_reach_json_payload``).
The record's message is the literal ``"http.request"`` so the log-based
metric can filter on ``jsonPayload.message = "http.request"``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import MutableMapping
from typing import Any

from starlette.types import ASGIApp as ASGIApplication
from starlette.types import Receive, Scope, Send

from core_api.services.capability_usage import record_usage

logger = logging.getLogger("core_api.access")

# REST route-template → (capability, op) for the adoption signal. Keyed by
# (METHOD, templated path) so the same path under different verbs maps to the
# right capability/op. Capability + op names are kept aligned with the MCP
# tool vocabulary (mcp_server tool names minus the ``memclaw_`` prefix, and
# the manage/doc sub-ops) so REST and MCP roll up together in the report.
#
# Routes NOT in this map are simply not recorded as capability usage (admin,
# list/registry, ingest pipeline, health, plugin bootstrap). Extend this when
# a new capability-bearing route is added — it's the REST half of the
# transport-agnostic taxonomy; the MCP half is automatic via call_tool.
_REST_CAPABILITY: dict[tuple[str, str], tuple[str, str | None]] = {
    # memories
    ("POST", "/api/v1/memories"): ("write", None),
    ("GET", "/api/v1/memories"): ("list", None),
    ("GET", "/api/v1/memories/stats"): ("stats", None),
    ("POST", "/api/v1/memories/bulk-delete"): ("manage", "bulk_delete"),
    ("DELETE", "/api/v1/memories"): ("manage", "bulk_delete"),
    ("GET", "/api/v1/memories/{memory_id}"): ("manage", "read"),
    ("GET", "/api/v1/memories/{memory_id}/contradictions"): ("manage", "read"),
    ("DELETE", "/api/v1/memories/{memory_id}"): ("manage", "delete"),
    ("PATCH", "/api/v1/memories/{memory_id}/status"): ("manage", "transition"),
    ("PATCH", "/api/v1/memories/{memory_id}"): ("manage", "update"),
    ("POST", "/api/v1/search"): ("search", None),
    ("POST", "/api/v1/recall"): ("recall", None),
    # documents
    ("POST", "/api/v1/documents"): ("doc", "write"),
    ("GET", "/api/v1/documents"): ("doc", "read"),
    ("GET", "/api/v1/documents/{doc_id}"): ("doc", "read"),
    ("GET", "/api/v1/documents/collections"): ("doc", "list_collections"),
    ("POST", "/api/v1/documents/query"): ("doc", "query"),
    ("POST", "/api/v1/documents/search"): ("doc", "search"),
    ("DELETE", "/api/v1/documents/{doc_id}"): ("doc", "delete"),
    # keystones (router prefix /memclaw/keystones)
    ("GET", "/api/v1/memclaw/keystones"): ("keystones", None),
    ("POST", "/api/v1/memclaw/keystones"): ("keystones_set", "set"),
    ("DELETE", "/api/v1/memclaw/keystones/{doc_id}"): ("keystones_set", "delete"),
    # knowledge graph / entities
    ("GET", "/api/v1/entities"): ("entity", "list"),
    ("GET", "/api/v1/graph"): ("entity", "graph"),
    ("POST", "/api/v1/entities/upsert"): ("entity", "write"),
    ("GET", "/api/v1/entities/{entity_id}"): ("entity", "read"),
    ("POST", "/api/v1/relations/upsert"): ("entity", "write"),
    # insights / evolve / stats / tune
    ("POST", "/api/v1/insights/generate"): ("insights", None),
    ("POST", "/api/v1/evolve/report"): ("evolve", None),
    ("GET", "/api/v1/stats"): ("stats", None),
    ("GET", "/api/v1/agents/{agent_id}/tune"): ("tune", "read"),
    ("PATCH", "/api/v1/agents/{agent_id}/tune"): ("tune", "update"),
}


class RequestObservationMiddleware:
    """ASGI middleware that emits one ``http.request`` event per request."""

    def __init__(self, app: ASGIApplication) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Lifespan / websocket — nothing to observe here.
            await self.app(scope, receive, send)
            return

        # Default to 500: if the downstream app raises before sending
        # ``http.response.start`` we still emit an event, and a crash that
        # never produced a status line is most accurately reported as 5xx.
        status_code = 500

        async def _send(message: MutableMapping[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        start = time.monotonic()
        try:
            await self.app(scope, receive, _send)
        finally:
            duration_ms = (time.monotonic() - start) * 1000.0
            # ``scope["route"]`` is set by the router during the call above;
            # read it now, never before. Fall back to "unmatched" so 404s and
            # pre-routing failures bucket into a single bounded label.
            route = scope.get("route")
            http_route = getattr(route, "path", None) or "unmatched"
            # ``request.state`` is backed by ``scope["state"]``; ``get_auth_context``
            # stashes ``tenant_id`` there once the caller is authenticated. Absent
            # for unauthenticated routes and 401s — logged as None, which is fine.
            state = scope.get("state") or {}
            tenant_id = state.get("tenant_id")
            method = scope.get("method", "?")
            logger.info(
                "http.request",
                extra={
                    "http_route": http_route,
                    "http_method": method,
                    "http_status_code": status_code,
                    "http_duration_ms": round(duration_ms, 1),
                    "tenant_id": tenant_id,
                },
            )
            # Adoption signal: record capability usage for mapped REST routes
            # (transport=rest). MCP traffic (POST /mcp) is recorded separately
            # by the call_tool wrapper, and /mcp isn't in the map, so there's
            # no double counting. record_usage is a no-op when the aggregator
            # isn't wired, skips non-tenant callers, and never raises.
            cap = _REST_CAPABILITY.get((method, http_route))
            if cap is not None:
                capability, op = cap
                record_usage(
                    capability=capability,
                    op=op,
                    transport="rest",
                    tenant_id=tenant_id,
                    status="ok" if status_code < 400 else "error",
                    duration_ms=duration_ms,
                )
