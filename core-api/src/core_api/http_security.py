"""Standalone HTTP authority policy and byte-safe credential comparisons."""

from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from core_api.config import settings


def credential_matches(presented: str, expected: str) -> bool:
    """Malformed credentials are a mismatch, including non-ASCII headers."""
    try:
        return hmac.compare_digest(presented.encode("ascii"), expected.encode("ascii"))
    except UnicodeEncodeError:
        return False


def transport_security_settings() -> TransportSecuritySettings:
    hosts = ["localhost", "127.0.0.1", "[::1]"]
    allowed_hosts = [*hosts, *(f"{host}:*" for host in hosts)]
    allowed_origins = [
        value
        for scheme in ("http", "https")
        for host in hosts
        for value in (f"{scheme}://{host}", f"{scheme}://{host}:*")
    ]
    allowed_hosts.extend(
        host.strip() for host in settings.standalone_allowed_hosts.split(",") if host.strip()
    )
    allowed_origins.extend(
        origin.strip() for origin in settings.standalone_allowed_origins.split(",") if origin.strip()
    )
    allowed_origins.extend(
        origin.strip() for origin in settings.cors_origins.split(",") if origin.strip() not in {"", "*"}
    )
    public = urlsplit(settings.public_api_url)
    if public.scheme in {"http", "https"} and public.netloc and not public.username and not public.password:
        allowed_hosts.append(public.netloc)
        allowed_origins.append(f"{public.scheme}://{public.netloc}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=settings.is_standalone,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


class StandaloneHTTPBoundary:
    """Check REST and mounted apps before standalone identity is resolved."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and settings.is_standalone:
            response = await TransportSecurityMiddleware(transport_security_settings()).validate_request(
                Request(scope)
            )
            if response is not None:
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
