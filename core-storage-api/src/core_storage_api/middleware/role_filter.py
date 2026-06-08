"""Reject unambiguous writes when the service is mounted as a reader
(CAURA-591 Part B).

Pure-ASGI middleware — matches ``RequestTimeoutMiddleware`` and the rest
of the codebase's middleware pattern rather than
``@app.middleware('http')``/``BaseHTTPMiddleware`` (which has known
cancellation edge cases through Starlette's anyio task groups).
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

# PATCH/PUT/DELETE are unambiguous writes. POST is intentionally NOT in
# this set: several POST endpoints (scored-search, semantic-duplicate,
# find-successors, entity-overlap-candidates, similar-candidates) are
# reads dressed as POST because the payload is too large for a query
# string, and the reader must still serve them.
_UNAMBIGUOUS_WRITE_METHODS = frozenset({"PATCH", "PUT", "DELETE"})

_BODY_405 = b'{"detail":"method not allowed on reader role"}'


class RejectWritesOnReaderMiddleware:
    """405 PATCH/PUT/DELETE; pass everything else through untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in _UNAMBIGUOUS_WRITE_METHODS:
            await self.app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 405,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_BODY_405)).encode()),
                    (b"allow", b"GET, POST, HEAD, OPTIONS"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _BODY_405, "more_body": False})
