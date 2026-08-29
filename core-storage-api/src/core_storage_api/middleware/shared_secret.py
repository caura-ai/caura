"""Fail-closed shared-secret boundary for core-storage-api."""

from __future__ import annotations

import hmac
import json

from starlette.types import ASGIApp, Receive, Scope, Send

from common.storage_auth import STORAGE_SHARED_SECRET_REJECTION_DETAIL

_BODY_401 = json.dumps({"detail": STORAGE_SHARED_SECRET_REJECTION_DETAIL}, separators=(",", ":")).encode()
_PUBLIC_HEALTH_PATHS = frozenset({"/healthz", "/readyz"})


class RequireStorageSharedSecretMiddleware:
    """Require the internal secret outside the public liveness probes."""

    def __init__(self, app: ASGIApp, *, shared_secret: str) -> None:
        self.app = app
        self.shared_secret = shared_secret.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope["method"] == "GET" and scope["path"] in _PUBLIC_HEALTH_PATHS:
            await self.app(scope, receive, send)
            return

        supplied = [value for name, value in scope.get("headers", []) if name.lower() == b"x-storage-secret"]
        if len(supplied) == 1 and self.shared_secret and hmac.compare_digest(supplied[0], self.shared_secret):
            await self.app(scope, receive, send)
            return

        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_BODY_401)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _BODY_401, "more_body": False})
