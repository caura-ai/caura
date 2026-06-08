"""Ingest body-size middleware (PR #9).

Rejects oversized requests to ``/ingest/preview``, ``/ingest/commit``, and
``/ingest/file`` before FastAPI parses the body — same pattern as
``RequestTimeoutMiddleware``: pure ASGI rather than ``BaseHTTPMiddleware``,
so it runs ahead of body parsing and pydantic validation.

A path-level ``Depends(...)`` doesn't work for this because FastAPI evaluates
body parameters and path dependencies together; an oversized payload returns
a 422 from body validation before our 413 dep ever fires. Middleware fixes
that ordering.

The check is cheap: read ``Content-Length`` from scope headers, return 413
if it exceeds the cap. If the header is missing (chunked transfer), fall
through — the per-route handler still re-checks the actual payload size for
defense in depth.
"""

from __future__ import annotations

import json
import logging

from starlette.types import ASGIApp as ASGIApplication
from starlette.types import Receive, Scope, Send

from core_api.services.ingest_service import INGEST_MAX_INPUT_BYTES

logger = logging.getLogger(__name__)

# Paths this middleware gates. We match by suffix (after the ``/api/v1``
# prefix is stripped, both styles can exist) so the same middleware works
# for the OSS and enterprise mount points without coupling to the
# include_router prefix used in app.py.
_GATED_PATH_SUFFIXES: tuple[str, ...] = (
    "/ingest/preview",
    "/ingest/commit",
    "/ingest/file",
)


def _is_gated(path: str) -> bool:
    return any(path.endswith(s) for s in _GATED_PATH_SUFFIXES)


class IngestBodySizeMiddleware:
    """ASGI middleware that 413s oversized ingest requests on Content-Length."""

    def __init__(self, app: ASGIApplication) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not _is_gated(scope["path"]):
            await self.app(scope, receive, send)
            return

        # Scope headers are a list of (bytes, bytes) tuples — Starlette's
        # canonical form. We don't construct a Request object to avoid
        # consuming the receive stream.
        cl_bytes: bytes | None = None
        for k, v in scope.get("headers", []):
            if k == b"content-length":
                cl_bytes = v
                break

        if cl_bytes is None:
            # No Content-Length (e.g. chunked transfer encoding). The route
            # handler still enforces the cap on the actual payload, so we
            # let this through — middleware just makes the common case cheap.
            await self.app(scope, receive, send)
            return

        try:
            n = int(cl_bytes.decode("ascii", errors="ignore"))
        except ValueError:
            # Malformed Content-Length — fall through; route guards still apply.
            await self.app(scope, receive, send)
            return

        if n > INGEST_MAX_INPUT_BYTES:
            max_mb = INGEST_MAX_INPUT_BYTES // 1_000_000
            payload = {
                "detail": (
                    f"File must be {max_mb} MB or under (got {n:,} bytes, max {INGEST_MAX_INPUT_BYTES:,})."
                )
            }
            body = json.dumps(payload).encode()
            logger.info(
                "ingest body-size cap fired: path=%s content_length=%d (max %d)",
                scope["path"],
                n,
                INGEST_MAX_INPUT_BYTES,
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": body,
                    "more_body": False,
                }
            )
            return

        await self.app(scope, receive, send)
