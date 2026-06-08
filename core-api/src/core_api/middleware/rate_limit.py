"""Rate limiting — slowapi-backed, Redis-or-memory.

Uses Redis (``settings.redis_url``) when configured — gives distributed
enforcement across Cloud Run instances — and falls back to an
in-process store otherwise so OSS standalone deployments work
unchanged.

Keying precedence: API key (preferred, stable across IPs) → remote IP.
Per-tenant and per-agent-key keying is a follow-up (tracked separately);
see the enterprise gateway nginx.conf per-IP limits for the current
coarse fallback.

Exported decorators are applied surgically to the hot-path routes that
the loadtest showed as unprotected:

- ``write_limit`` — POST /memories, POST /documents
- ``write_bulk_limit`` — POST /memories/bulk (stricter — 100x fanout)
- ``search_limit`` — POST /search, POST /recall
"""

from __future__ import annotations

import hashlib

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from core_api.config import settings

# `limits` library storage URIs: "memory://", "redis://host:port/db".
# An empty redis_url (OSS default) → memory store, single-instance only.
_STORAGE_URI = settings.redis_url or "memory://"


def _key_func(request: Request) -> str:
    """Rate-limit key. Prefers API key over IP so NAT'd agents don't
    cannibalise each other's budget.

    The API key is hashed so the full secret never lands in the
    storage backend or access logs, while keeping buckets unique for
    keys that happen to share a prefix.
    """
    api_key = request.headers.get("x-api-key")
    if not api_key:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            api_key = auth[len("Bearer ") :]
    if api_key:
        return f"key:{hashlib.sha256(api_key.encode()).hexdigest()[:32]}"
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(
    key_func=_key_func,
    storage_uri=_STORAGE_URI,
    # Redis outages degrade gracefully — requests pass through rather
    # than all rate-limited routes returning 500.
    swallow_errors=True,
    # No default_limits — decorators are applied explicitly per-route.
    # Avoids accidentally limiting /health, /version, /mcp, etc.
)


write_limit = limiter.limit(settings.rate_limit_write)
write_bulk_limit = limiter.limit(settings.rate_limit_write_bulk)
search_limit = limiter.limit(settings.rate_limit_search)
