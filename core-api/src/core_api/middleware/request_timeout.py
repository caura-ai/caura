"""Request-wide timeout middleware (CAURA-600).

Pure ASGI middleware — matches the ``SecurityHeadersMiddleware`` pattern
rather than ``@app.middleware('http')`` (which wraps in
``BaseHTTPMiddleware`` and has known edge cases around cancellation
through Starlette's anyio task groups).

Caveat: ``asyncio.timeout`` cannot cancel synchronous threads started
via ``asyncio.to_thread`` (used by the Vertex and Gemini provider
SDKs). The deadline frees the request slot and returns 504 to the
caller on time, but the background thread may outlive the budget up
to the provider's own latency and hold a ThreadPoolExecutor slot.
Moving those calls off the hot path (CAURA-594 / CAURA-595) is the
real fix; this middleware is the outer safety net.
"""

import asyncio
import logging
from collections.abc import MutableMapping
from typing import Any

from starlette.types import ASGIApp as ASGIApplication
from starlette.types import Receive, Scope, Send

from core_api.constants import is_mcp_path

logger = logging.getLogger(__name__)

# Routes that opt out of the blanket middleware budget and enforce their
# own deadline at the route layer. Bulk-write (CAURA-602) needs a longer
# budget than the single-write hot path, and cancelling it from this
# middleware *after* storage commits is exactly what produced the
# silent-create regression in loadtest-1777301515 — the route now wraps
# its own ``asyncio.wait_for`` and surfaces the 504 with the retry
# contract intact.
# ``/admin/org/purge-data`` (CAURA-689) hard-deletes every row of a
# soft-deleted org across the OSS schema — a terminal admin batch op
# driven by the daily sweep, not a user request. A large org can take
# well past the hot-path budget; cancelling it mid-flight is pointless
# (the purge is one transaction per tenant and idempotent on retry), so
# it opts out and is bounded by the storage client's own timeout instead.
# ``/interview/submit`` (Interviewer Phase 1) runs a synchronous
# map-reduce LLM interview — a realistic window measured ~63s in the
# real-LLM pilot, well past the blanket budget. Like bulk, the route
# enforces its own deadline (``interview_request_timeout_seconds``) and
# its failure mode is retry-safe (watermark advances only post-commit;
# the plugin never prunes on error; the attempt id dedups).
_TIMEOUT_OPT_OUT_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/memories/bulk",
        "/api/v1/admin/org/purge-data",
        "/api/v1/interview/submit",
    }
)


def _is_opted_out(path: str) -> bool:
    # Exact match keeps neighbours like ``/memories/bulk-delete`` on the
    # default budget. ASGI ``scope["path"]`` excludes the querystring, so
    # no startswith-with-``?`` fallback is needed.
    return path in _TIMEOUT_OPT_OUT_PATHS


class RequestTimeoutMiddleware:
    def __init__(self, app: ASGIApplication, timeout_seconds: float) -> None:
        self.app = app
        self.timeout_seconds = timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or is_mcp_path(scope["path"]) or _is_opted_out(scope["path"]):
            await self.app(scope, receive, send)
            return

        response_started = False

        async def _send(message: MutableMapping[str, Any]) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self.app(scope, receive, _send)
        except TimeoutError:
            logger.warning(
                "request exceeded %ss budget: %s %s",
                self.timeout_seconds,
                scope.get("method", "?"),
                scope["path"],
            )
            if response_started:
                # Headers already sent; synthesizing a 504 here would leave
                # the response body half-written. Let the ASGI server drop
                # the connection via normal cancellation propagation.
                raise
            await send(
                {
                    "type": "http.response.start",
                    "status": 504,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"detail":"request timeout"}',
                    "more_body": False,
                }
            )
