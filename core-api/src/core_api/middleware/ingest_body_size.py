"""Ingest body-size middleware (PR #9).

Rejects oversized requests to ``/ingest/preview``, ``/ingest/commit``, and
``/ingest/file`` before FastAPI parses the body — same pattern as
``RequestTimeoutMiddleware``: pure ASGI rather than ``BaseHTTPMiddleware``,
so it runs ahead of body parsing and pydantic validation.

A path-level ``Depends(...)`` doesn't work for this because FastAPI evaluates
body parameters and path dependencies together; an oversized payload returns
a 422 from body validation before our 413 dep ever fires. Middleware fixes
that ordering.

The check rejects an oversized ``Content-Length`` immediately, then buffers at
most the capped body before handing it to FastAPI. Counting the actual ASGI
body closes the chunked-transfer and dishonest-header bypasses while keeping
memory usage bounded by the same 3 MB limit the routes advertise.
"""

from __future__ import annotations

import json
import logging

from starlette.types import ASGIApp as ASGIApplication
from starlette.types import Message, Receive, Scope, Send

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
    """ASGI middleware that 413s oversized ingest request bodies."""

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

        declared_size: int | None = None
        if cl_bytes is not None:
            try:
                declared_size = int(cl_bytes.decode("ascii", errors="ignore"))
            except ValueError:
                # Treat a malformed header like an absent one: the actual-body
                # counter below remains authoritative.
                pass

        if declared_size is not None and declared_size > INGEST_MAX_INPUT_BYTES:
            await self._reject(send, scope["path"], declared_size)
            return

        body = bytearray()
        disconnected = False
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
                if len(body) > INGEST_MAX_INPUT_BYTES:
                    await self._reject(send, scope["path"], len(body))
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                disconnected = True
                break

        replay: list[Message] = []
        if body:
            replay.append(
                {
                    "type": "http.request",
                    "body": bytes(body),
                    "more_body": disconnected,
                }
            )
        if disconnected:
            replay.append({"type": "http.disconnect"})
        elif not replay:
            replay.append({"type": "http.request", "body": b"", "more_body": False})

        next_message = iter(replay)

        async def replay_receive() -> Message:
            try:
                return next(next_message)
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(send: Send, path: str, size: int) -> None:
        """Emit the route's stable 413 response without invoking FastAPI."""
        max_mb = INGEST_MAX_INPUT_BYTES // 1_000_000
        payload = {
            "detail": (
                f"File must be {max_mb} MB or under (got {size:,} bytes, max {INGEST_MAX_INPUT_BYTES:,})."
            )
        }
        body = json.dumps(payload).encode()
        logger.info(
            "ingest body-size cap fired: path=%s body_bytes=%d (max %d)",
            path,
            size,
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
