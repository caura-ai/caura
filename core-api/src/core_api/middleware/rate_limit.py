"""Rate limiting — slowapi-backed, Redis-or-memory.

Uses Redis (``settings.redis_url``) when configured — gives distributed
enforcement across Cloud Run instances — and falls back to an
in-process store otherwise so OSS standalone deployments work
unchanged.

Keying: the bucket auth named (``request.state.rate_limit_key``, L-69), hashed,
else the remote IP. Auth names it after what it verified: the admin key or
``CAURA_API_KEY``, or behind the gateway the credential the gateway accepted
(``X-Caura-Credential-ID``), or else the identity it set (see
``gateway_bucket_name``). A header nothing checked never names a bucket, since a
fresh one per request would be a fresh budget. In every hosted environment auth
names a bucket for each request it admits (core-api will not boot there without
a gateway secret or ``CAURA_API_KEY``), so the IP fallback is for development
and standalone, where behind a proxy every caller shares the proxy's address.

Each limit also has a tenant-wide bucket, ``tenant:<id>``, at its
``*_tenant`` rate: an org may hold hundreds of credentials, each with its own
budget. A request auth resolved no tenant for (the admin key) pays only its
credential's.

Exported decorators are applied surgically to the hot-path routes that
the loadtest showed as unprotected:

- ``write_limit`` — POST /memories, POST /documents, POST /stm/promote (an LTM
  write through the STM door; it pays the same limit as the front door)
- ``write_bulk_limit`` — POST /memories/bulk, POST /ingest/commit (stricter —
  100x fanout). One budget across both, shared with MCP batch writes.
- ``search_limit`` — POST /search, POST /recall, POST /documents/search,
  GET /memories and the other read routes
- ``llm_limit`` — the routes that call a model per request: POST
  /ingest/preview, /ingest/file, /interview/submit, /insights/generate,
  /evolve/report. One budget across all of them, shared with their MCP twins.

``write_limit`` and ``search_limit`` keep a bucket per route, as before;
``write_bulk_limit`` and ``llm_limit`` are one bucket per credential across
their routes. MCP tool calls are charged in ``mcp_server`` through
``charge_call``, against the shared buckets above or a bucket per tool. Rates
are read from settings on each request, not when a route is decorated.

Every decorated handler must declare a ``response: Response`` parameter:
with ``headers_enabled=True`` (below) slowapi injects the rate-limit
headers into it on the SUCCESS path, and raises without one. See D14 and
``tests/test_d14_rate_limited_response_param.py``, which fails any new
rate-limited route that forgets it.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from collections.abc import Callable
from typing import Literal

from fastapi import Request
from slowapi import Limiter
from slowapi.wrappers import LimitGroup

from core_api.config import settings

logger = logging.getLogger(__name__)

Kind = Literal["write", "write_bulk", "search", "llm"]

# Each kind's per-credential rate, then its tenant-wide rate, as setting names.
_RATES: dict[Kind, tuple[str, str]] = {
    "write": ("rate_limit_write", "rate_limit_write_tenant"),
    "write_bulk": ("rate_limit_write_bulk", "rate_limit_write_bulk_tenant"),
    "search": ("rate_limit_search", "rate_limit_search_tenant"),
    "llm": ("rate_limit_llm", "rate_limit_llm_tenant"),
}

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
    """Rate-limit key: the bucket auth named, else the client IP. Naming it
    after a credential or identity rather than the IP keeps NAT'd agents from
    cannibalising each other's budget.

    The name is hashed so a credential never lands in the storage backend
    or access logs, while keeping buckets unique for keys that happen to
    share a prefix.
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

    # Never the raw key headers (L-69): standalone checks no key, header trust
    # without a gateway secret takes the caller's own headers, and behind the
    # gateway core-api cannot tell which credential was accepted. slowapi runs
    # this inside the route, after the auth dependency, so any name is set.
    client = request.client.host if request.client else None
    return bucket_key(getattr(request.state, "rate_limit_key", None), client)


def bucket_key(rate_limit_key: str | None, client_host: str | None) -> str:
    """The credential bucket: what auth named, hashed, else the client address.

    Shared by the route decorators and MCP's ``call_tool``, so a credential is
    one bucket on both transports. The address fallback is slowapi's
    ``get_remote_address``, ``127.0.0.1`` included.
    """
    if rate_limit_key:
        return f"key:{hashlib.sha256(rate_limit_key.encode()).hexdigest()[:32]}"
    return f"ip:{client_host or '127.0.0.1'}"


def gateway_bucket_name(header: Callable[[str], str | None], tenant_id: str) -> str:
    """The bucket for a request the gateway authenticated (behind its secret).

    The gateway's auth subrequest tries a JWT bearer, then the session cookie,
    then ``X-API-Key``, and forwards all of them unchanged, so which of them a
    request carries says nothing about which one it accepted. It does say: it
    sets ``X-Caura-Credential-ID`` to the API key it accepted, on every request,
    overwriting the client's. That id names the bucket. A request it accepted
    no key for (a session or a JWT) gets the identity it set the same way:
    tenant, user, agent and install.

    ``header`` reads one request header by its lower-case name.
    """
    credential_id = (header("x-caura-credential-id") or "").strip()
    if credential_id:
        return f"credential-id:{credential_id}"
    identity = [header(h) or "" for h in ("x-user-id", "x-agent-id", "x-install-uuid")]
    return "identity:" + "|".join([tenant_id, *identity])


def _tenant_key_func(request: Request) -> str:
    return f"tenant:{request.state.tenant_id}"


def _no_tenant(request: Request) -> bool:
    """Auth resolved no tenant (the admin key): there is no tenant bucket."""
    return not getattr(request.state, "tenant_id", None)


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
    # D14 — without this, slowapi's ``_inject_headers`` is a silent no-op
    # (it checks ``self._headers_enabled`` first), so NO response carried
    # X-RateLimit-* and no 429 carried Retry-After — despite app.py's 429
    # handler dutifully calling the injector. Field-observed as "MCP feels
    # fragile": throttled agents saw random unreliability with no back-off
    # signal (Hermes onboarding test).
    headers_enabled=True,
    # No default_limits — decorators are applied explicitly per-route.
    # Avoids accidentally limiting /health, /version, /mcp, etc.
)


def _route_limit(kind: Kind, *, shared: bool) -> Callable:
    """A decorator charging ``kind``'s per-credential and tenant buckets.

    ``shared``: one bucket per credential (and per tenant) across every route
    carrying it, named ``kind``; otherwise a bucket per route.

    slowapi files a limit under the route's function name, and one wrapper
    checks every limit filed there, in the order filed. So the route gets the
    credential limit's wrapper alone, filed first, and the tenant limit is only
    filed: each wrapper sends the rate-limit headers, so a second would send
    them twice. Filed first, the credential limit is the one the headers report,
    and ``_key_func`` runs before any bucket is hit, which its fail-open seed
    relies on.
    """
    credential_rate, tenant_rate = _RATES[kind]

    def _setting(name: str) -> Callable[[], str]:
        return lambda: getattr(settings, name)

    if shared:
        per_credential = limiter.shared_limit(_setting(credential_rate), scope=kind)
        per_tenant = limiter.shared_limit(
            _setting(tenant_rate), scope=kind, key_func=_tenant_key_func, exempt_when=_no_tenant
        )
    else:
        per_credential = limiter.limit(_setting(credential_rate))
        per_tenant = limiter.limit(_setting(tenant_rate), key_func=_tenant_key_func, exempt_when=_no_tenant)

    def apply(func: Callable) -> Callable:
        wrapped = per_credential(func)
        per_tenant(func)
        return wrapped

    return apply


write_limit = _route_limit("write", shared=False)
write_bulk_limit = _route_limit("write_bulk", shared=True)
search_limit = _route_limit("search", shared=False)
llm_limit = _route_limit("llm", shared=True)


def charge_call(kind: Kind, *, bucket: str | None, tenant_id: str, scope: str) -> int | None:
    """Charge one call outside a route to ``kind``'s buckets: ``bucket`` (the
    caller's, from ``bucket_key``) and its tenant's.

    ``scope`` names the buckets: ``kind`` itself for the shared ones, so the
    call draws on the same budget as the routes carrying that limit, or the
    caller's own name for a bucket of its own. Returns the seconds until the
    spent bucket refills, or None when the call may go ahead.

    Behaves as the decorators do: nothing while the limiter is disabled, and
    fail-open when the store errors (``swallow_errors``).
    """
    if not limiter.enabled:
        return None
    credential_rate, tenant_rate = _RATES[kind]
    charges = [(getattr(settings, tenant_rate), f"tenant:{tenant_id}")]
    if bucket:
        charges.insert(0, (getattr(settings, credential_rate), bucket))
    try:
        for rate, key in charges:
            # Parsed as slowapi parses a decorator's (``memory_field`` does the
            # same). ``(key, scope)`` is the order slowapi passes them in, so a
            # shared bucket here is the routes' own.
            group = LimitGroup(
                rate,
                key_function=_key_func,
                scope=scope,
                per_method=False,
                methods=None,
                error_message=None,
                exempt_when=None,
                cost=1,
                override_defaults=True,
            )
            for lim in group:
                if not limiter.limiter.hit(lim.limit, key, scope):
                    reset_at, _ = limiter.limiter.get_window_stats(lim.limit, key, scope)
                    return max(1, math.ceil(reset_at - time.time()))
    except Exception:
        logger.warning("Failed to rate limit a call; letting it through", exc_info=True)
    return None
