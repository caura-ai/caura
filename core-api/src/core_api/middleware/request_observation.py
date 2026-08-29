"""Request-observation middleware — per-endpoint API usage metrics.

Pure ASGI middleware — matches the ``SecurityHeadersMiddleware`` /
``RequestTimeoutMiddleware`` / ``IngestBodySizeMiddleware`` pattern rather
than ``BaseHTTPMiddleware`` (which wraps in Starlette's task groups and has
known edge cases around cancellation and streaming responses).

Emits at most one structured ``http.request`` log event per HTTP request,
carrying the *templated* route, method, status, and wall-clock duration.
These events back a log-based distribution metric for per-endpoint
API-usage dashboards on Cloud-Logging deployments; on file-logging
deployments they land in the JSON log file just like any other log line.

"At most" rather than "exactly": a liveness/readiness probe that SUCCEEDS
is not logged, because it is infrastructure chatter rather than API usage.
A probe that FAILS still is. See ``PROBE_ROUTES`` for the routes, the
rationale, and what that costs.

Cardinality contract
--------------------
``http_route`` is ALWAYS one of three bounded things, in this order:

1. the route template from ``scope["route"].path`` — set by FastAPI's
   ``APIRoute``. Note this is the template as the ROUTER declares it, with
   no router prefix on it: a GET of ``/api/v1/memories/abc`` is labelled
   ``/memories/{memory_id}``. Since FastAPI 0.137,
   ``include_router(prefix=...)`` mounts the router instead of flattening
   it, so the prefix lives in ``root_path`` and never reaches this label;
2. the mount prefix (``/mcp``, ``/static``) for traffic served by a
   mounted sub-app, which is NOT a FastAPI route and so never populates
   ``scope["route"]``;
3. the literal ``"unmatched"`` — genuine 404s and failures before
   routing.

It is NEVER the raw request path with path params interpolated — raw
paths would explode the metric's label cardinality and make it
expensive. Mount prefixes are safe on that count because mounts are
declared statically in ``app.py``: one extra label each.

Case 2 was folded into case 3 until 2026-08-29, which made ~40% of prod
events unattributable and hid MCP traffic from the per-endpoint
dashboard entirely — see the fallback in ``__call__`` for the numbers.

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

from core_api.constants import PROBE_ROUTES
from core_api.services.capability_usage import record_usage

logger = logging.getLogger("core_api.access")

# Liveness/readiness probe routes, whose SUCCESSFUL requests are not logged.
#
# ``PROBE_ROUTES`` is imported rather than restated because ``routes/health.py``
# builds its route decorators from the very same constants — one string backs
# both, so the endpoint cannot be renamed out from under this check. See the
# note beside it in ``constants.py`` for why the values are router-relative
# (``/health``, not ``/api/v1/health``).
#
# Why drop them: Cloud Run probes each instance on a fixed interval, so
# these arrive at a near-constant rate no matter what real traffic does —
# ~5.4k/day in prod, of which 100% over 7 days to 2026-08-29 (37,850 events)
# were untenanted infrastructure calls, never a real API consumer. As a
# SHARE of this metric they are wildly unstable for the same reason: 1.3% on
# a busy day, 12.5% on a quiet one. The stable number is the absolute rate.
#
# Why only the successful ones: a probe that returns 200 answers no question
# this metric exists to answer, but a failing one is exactly what an
# incident investigation reaches for — prod served 440 ``/health`` 503s in
# that same week. Suppressing by route alone would have thrown all 440 away
# to save 1.2% more volume. Non-2xx probes stay.
#
# What this gives up: "are probes arriving at all?" can no longer be
# answered from these logs, since silence now means both "healthy" and
# "nothing is calling us". Cloud Run reports instance health directly, and a
# probe that fails still logs, so that gap is covered elsewhere.
#
# And the denominator for these two routes is now gone, so a per-route ERROR
# RATE (failures / total) is meaningless for them — every probe event that
# survives is a failure, so any such ratio reads 100% the moment one 503
# lands. For PROBE_ROUTES read the absolute failure COUNT instead. Nothing
# in-repo derives a rate from this metric today; the point is that an alert
# added later must not, and an on-call engineer seeing "100% errors on
# /health" should recognise it as this suppression rather than an outage.

# REST route-template → (capability, op) for the adoption signal. Keyed by
# (METHOD, templated path) so the same path under different verbs maps to the
# right capability/op. Capability + op names are kept aligned with the MCP
# tool vocabulary (mcp_server tool names minus the ``caura_`` prefix, and
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
    # Canonical brand-neutral paths (2026-08-14) + the permanent legacy alias.
    ("GET", "/api/v1/keystones"): ("keystones", None),
    ("POST", "/api/v1/keystones"): ("keystones_set", "set"),
    ("DELETE", "/api/v1/keystones/{doc_id}"): ("keystones_set", "delete"),
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
    """ASGI middleware emitting at most one ``http.request`` event per request.

    "At most" because a SUCCESSFUL liveness/readiness probe emits none; see
    ``PROBE_ROUTES``. Every other request, failed probes included, emits one.
    """

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

        # Captured BEFORE the downstream call: a Starlette ``Mount`` appends
        # its prefix to ``scope["root_path"]``, so the delta across the call
        # is the mount prefix and nothing else. Subtracting an app-level
        # root_path any other way misfires — with the app served under one
        # (``--root-path /gw``), a genuine 404 would otherwise be labelled
        # ``/gw``. Verified against the locked fastapi 0.141.1 / starlette
        # 1.3.1, with and without an app root_path.
        root_before = scope.get("root_path") or ""
        start = time.monotonic()
        try:
            await self.app(scope, receive, _send)
        finally:
            duration_ms = (time.monotonic() - start) * 1000.0
            # ``scope["route"]`` is set by the router during the call above;
            # read it now, never before. Fall back to "unmatched" so 404s and
            # pre-routing failures bucket into a single bounded label.
            route = scope.get("route")
            http_route = getattr(route, "path", None)
            if not http_route:
                # Only FastAPI's APIRoute sets ``scope["route"]``; a Starlette
                # ``Mount`` never does (starlette 1.3.1 has no such assignment
                # at all). So every request into a mounted sub-app — app.py
                # mounts ``/mcp`` and ``/static`` — fell into "unmatched"
                # beside genuine 404s. In 6h of prod on 2026-08-29 that was
                # 5,027 of 5,099 unmatched events (98.6%), every one of them
                # SUCCESSFUL: ~40% of this metric's volume was unattributable
                # and the MCP transport was invisible in the per-endpoint
                # dashboard, while "unmatched" looked like a 404 problem.
                #
                # Labelling by mount prefix keeps the cardinality contract
                # above: mounts are declared statically in app.py, so this
                # adds one bounded label per mount, never a raw path. Genuine
                # 404s and pre-routing failures have no mount and still read
                # "unmatched", which now means what the contract says.
                #
                # This leans on two Starlette internals — that ``Mount``
                # mutates THIS scope dict in place, and that it never sets
                # ``scope["route"]`` — neither of which the ASGI spec
                # guarantees, and starlette is not pinned directly here (only
                # transitively, via ``fastapi>=0.141.1,<1``). Two things make
                # that acceptable. It degrades safely: if either stops
                # holding, the delta is empty and the label falls back to
                # "unmatched", i.e. exactly today's behaviour, never a wrong
                # or unbounded label. And it degrades LOUDLY in CI rather
                # than silently in prod, because
                # ``tests/test_request_observation.py`` drives real
                # ``app.mount()`` calls through this middleware over a real
                # ASGI transport — nothing about the framework is mocked — so
                # a dependency bump that changes either behaviour turns those
                # tests red. Keep them that way; a mock there would give the
                # pin-free dependency a silent path back in.
                root_after = scope.get("root_path") or ""
                http_route = (
                    root_after[len(root_before) :] if root_after.startswith(root_before) else ""
                ) or "unmatched"
            # ``request.state`` is backed by ``scope["state"]``; ``get_auth_context``
            # stashes ``tenant_id`` there once the caller is authenticated. Absent
            # for unauthenticated routes and 401s — logged as None, which is fine.
            state = scope.get("state") or {}
            tenant_id = state.get("tenant_id")
            method = scope.get("method", "?")
            # A probe that SUCCEEDED tells us nothing this metric exists to
            # answer, so don't pay to store it. One that failed is exactly
            # what an investigation wants, so it still goes out — see
            # ``PROBE_ROUTES``. Guard the emit with a condition rather than
            # returning early: this is a ``finally`` block, and a ``return``
            # here would swallow an in-flight exception from the downstream
            # call.
            if not (http_route in PROBE_ROUTES and status_code < 400):
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
