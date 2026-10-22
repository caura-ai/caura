"""Standalone tenant injection: no Bearer token opts a request out of it.

``StandaloneTenantMiddleware`` used to skip the injection when the request
carried an HS256 JWT, signed with ``jwt_secret``, whose payload said
``super_admin``. Nothing has issued that claim since caura-enterprise retired its
super-admin stack (6ebb0cb6, 2026-05-03; the surface moved to CauraOps, under
Google auth), and core-api issues no JWTs of its own. The only token that could
still take the path was one signed by whoever holds ``jwt_secret``, which a
standalone install leaves at ``change-me-in-production``. The check was also
core-api's only use of python-jose, whose CVE-2026-85394 has no fixed release.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from core_api.config import settings
from core_api.middleware import standalone_tenant
from core_api.middleware.standalone_tenant import StandaloneTenantMiddleware

TENANT = "standalone-tenant"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _hs256_jwt(payload: dict, secret: str) -> str:
    """An HS256 JWT built by hand, so the test needs no JWT library."""
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode())
        for part in ({"alg": "HS256", "typ": "JWT"}, payload)
    )
    signature = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


@pytest.mark.parametrize("super_admin_jwt", [True, False], ids=["super-admin-jwt", "opaque-bearer"])
async def test_every_bearer_request_gets_the_standalone_tenant(monkeypatch, super_admin_jwt: bool) -> None:
    monkeypatch.setattr(standalone_tenant, "get_standalone_tenant_id", lambda: TENANT)
    token = _hs256_jwt({"super_admin": True}, settings.jwt_secret) if super_admin_jwt else "not-a-jwt"
    seen: dict[str, bytes] = {}

    async def app(scope, receive, send):
        seen["query_string"] = scope["query_string"]

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/memories",
        "query_string": b"",
        "headers": [(b"authorization", f"Bearer {token}".encode())],
    }
    await StandaloneTenantMiddleware(app)(scope, receive, send)

    assert seen["query_string"] == f"tenant_id={TENANT}".encode()
