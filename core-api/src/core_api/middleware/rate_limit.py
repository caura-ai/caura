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

# Redis connection resilience (cuts the "Failed to rate limit. Swallowing
# error" ConnectionReset tail seen in prod): forwarded to the redis client
# behind the limiter. ``health_check_interval`` PINGs a connection idle >30s
# before reuse so one the server already closed (Memorystore maintenance /
# single-zone failover, or idle eviction) is detected + reconnected rather
# than reset mid-command; ``socket_keepalive`` keeps the TCP alive so
# intermediaries don't drop it; the bounded connect/op timeouts fail fast into
# the ``swallow_errors`` fail-open path instead of hanging. Empty for the
# in-memory store (memory://), which takes no connection options.
_STORAGE_OPTIONS: dict[str, object] = (
    {
        "socket_keepalive": True,
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
        "health_check_interval": 30,
    }
    if settings.redis_url
    else {}
)


def _key_func(request: Request) -> str:
    """Rate-limit key. Prefers API key over IP so NAT'd agents don't
    cannibalise each other's budget.

    The API key is hashed so the full secret never lands in the
    storage backend or access logs, while keeping buckets unique for
    keys that happen to share a prefix.
    """
    # Fail-open seed for slowapi 0.1.10's swallow_errors gap. slowapi sets
    # ``request.state.view_rate_limit`` only AFTER it hits the storage backend
    # (extension.py: ``self.limiter.hit(...)`` then ``view_rate_limit = ...``).
    # With ``swallow_errors=True`` a Redis outage is swallowed BEFORE that
    # assignment ("Failed to rate limit. Swallowing error"), yet the limit
    # decorator then reads ``request.state.view_rate_limit`` unconditionally to
    # inject headers — so the swallow path raised AttributeError → 500 instead of
    # failing open (prod 2026-06-17: Redis connection reset → 500s on
    # /api/v1/search). key_func runs first in every check and never touches the
    # backend, so seed None here: a successful check overwrites it, a swallowed
    # one leaves it None — which slowapi's ``_inject_headers`` no-ops on (verified
    # 0.1.10 extension.py: ``if ... and current_limit is not None``; pinned by
    # test_inject_headers_is_noop_for_none_limit so a slowapi upgrade that breaks
    # it fails CI, not prod) — making ``swallow_errors`` actually fail open.
    #
    # Guard with ``hasattr``: slowapi calls key_func once PER applied limit (and
    # per limit in a multi-part "10/s;100/h" string), so an unconditional seed
    # would reset a value an earlier limit's successful ``hit()`` already wrote.
    # Only seed when unset — the attribute still always exists before the first
    # ``hit()``, so the fail-open guarantee holds.
    if not hasattr(request.state, "view_rate_limit"):
        request.state.view_rate_limit = None

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
    # slowapi types storage_options as dict[str, str], but it forwards them to
    # the limits → redis client, which wants bool/int for these connection
    # kwargs (socket_keepalive, *_timeout, health_check_interval).
    storage_options=_STORAGE_OPTIONS,  # type: ignore[arg-type]
    # Redis outages degrade gracefully — requests pass through rather
    # than all rate-limited routes returning 500.
    swallow_errors=True,
    # No default_limits — decorators are applied explicitly per-route.
    # Avoids accidentally limiting /health, /version, /mcp, etc.
)


write_limit = limiter.limit(settings.rate_limit_write)
write_bulk_limit = limiter.limit(settings.rate_limit_write_bulk)
search_limit = limiter.limit(settings.rate_limit_search)
