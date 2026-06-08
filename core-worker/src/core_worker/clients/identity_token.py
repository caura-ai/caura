"""Cloud Run identity token fetching (CAURA-591 Part B Y3, ported to worker).

The core-worker calls ``core-storage-api`` (PATCH /memories/{id}) on every
embed-request and enrich-request. In the SaaS deploy the storage service
runs on Cloud Run with ``--no-allow-unauthenticated``, so the worker must
present a Cloud Run ID token bound to the storage service URL as audience.

This is a near-verbatim port of ``core_api.clients.identity_token`` —
same TTL caches, same per-audience locking, same lazy ``google.auth``
import so the OSS / local-dev path (no creds available) cleanly returns
``{}``. Two failure modes remain distinct:

1. ``google.auth`` not importable → permanent no-creds env. Cache the
   empty header at the full TTL so we don't re-attempt imports per call.
2. ``id_token.fetch_id_token(...)`` raised → could be a transient
   metadata-server hiccup. Cache an empty dict for the 30 s cooldown
   only so we recover quickly without stampeding the metadata server.

The sync ``google.auth`` call is offloaded via ``asyncio.to_thread``
so it doesn't block the event loop. Per-audience locks serialize
concurrent refreshes for the same audience without preventing the
worker's writer audience from blocking other audiences (none today,
but reserved for future read-replica splits).
"""

from __future__ import annotations

import asyncio
import logging

from cachetools import TTLCache  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

# The GCE metadata server is a local loopback that normally responds in
# <100 ms; 5 s leaves plenty of headroom for transient slowness while
# still failing fast enough that the 30 s failure cooldown below
# actually serves its purpose. Without this timeout, a network
# partition would hang asyncio.to_thread forever, pinning the
# per-audience lock and deadlocking concurrent requests.
_METADATA_SERVER_TIMEOUT_SECONDS = 5.0

# Google ID tokens are valid for 1 hour; cache 50 min so a token is
# never closer than 10 min to expiry when we send it. Max size 16 is
# well above the 1 audience (storage writer) we actually have today.
# Values are pre-built header dicts (or ``{}`` for permanent no-creds
# envs) so callers don't pay the allocation per request.
_cache: TTLCache[str, dict[str, str]] = TTLCache(maxsize=16, ttl=50 * 60)

# Short-TTL negative cache for transient fetch failures. 30 s keeps
# the metadata server from being hammered during an incident while
# still recovering within one refresh-polling cycle of a downstream
# alert. Separate from the main cache so the 50-min TTL on success
# doesn't also apply to failures.
_failure_cache: TTLCache[str, bool] = TTLCache(maxsize=16, ttl=30)

# Entries are never pruned — safe only because audiences are static
# config values (storage writer URL from settings). Dynamic audiences
# (blue/green deploys with shifting service URLs) would leak lock
# objects and need an eviction hook.
_audience_locks: dict[str, asyncio.Lock] = {}


class _NoCredentials:
    """Sentinel for the permanent no-creds case (google.auth not
    importable). Distinct from a raised exception, which indicates a
    transient fetch failure on an otherwise-credentialled environment."""


_NO_CREDS = _NoCredentials()


def _fetch_blocking(audience: str) -> str | _NoCredentials:
    """Sync call to the metadata server. Returns the token on success,
    the ``_NO_CREDS`` sentinel when ``google.auth`` isn't importable
    (permanent no-creds env), and raises on any fetch failure so the
    async wrapper can treat it as transient and avoid long caching."""
    try:
        import requests as _requests  # type: ignore[import-untyped]
        from google.auth.transport.requests import Request  # type: ignore[import-untyped]
        from google.oauth2 import id_token  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover — import-time only
        # Only catch actual missing-package errors. A broken-but-
        # installed google.auth (AttributeError, RuntimeError at
        # module init) would otherwise look like "no creds" and get
        # cached at the full 50-min TTL, turning a fixable prod bug
        # into an hour of 401s; let those propagate to the caller's
        # transient-failure path instead.
        logger.debug("google.auth not importable (no ID token): %s", exc)
        return _NO_CREDS

    # Session subclass rather than monkey-patching request.session.request:
    # that attribute is a google.auth internal, and a rename there would
    # silently restore the default-no-timeout behaviour. ``Request(session=)``
    # is the public API (stable since google-auth 2.x). We force our timeout
    # last via ``kwargs[...] = ...``; ``functools.partial`` would collide
    # with google.auth's explicit ``timeout=<default>`` kwarg and raise
    # ``TypeError: got multiple values for keyword argument 'timeout'``.
    class _TimeoutSession(_requests.Session):
        def request(self, *args, **kwargs):
            kwargs["timeout"] = _METADATA_SERVER_TIMEOUT_SECONDS
            return super().request(*args, **kwargs)

    # Use as a context manager so the urllib3 connection pool + file
    # descriptors are released after each fetch. A single refresh
    # happens ~once per hour per audience; no pool reuse benefit, just
    # potential FD leaks if we don't close.
    with _TimeoutSession() as session:
        return id_token.fetch_id_token(Request(session=session), audience)


def _lock_for(audience: str) -> asyncio.Lock:
    """Per-audience asyncio.Lock, lazily created. Safe to call without
    an outer lock — the dict is only mutated on the event loop and the
    get/set pair has no await points between them, so we can't race.
    ``setdefault`` would evaluate ``asyncio.Lock()`` on every call even
    on hit; this shape only allocates when we actually need a new lock."""
    lock = _audience_locks.get(audience)
    if lock is None:
        lock = asyncio.Lock()
        _audience_locks[audience] = lock
    return lock


async def fetch_auth_header(audience: str) -> dict[str, str]:
    """Return an ``{"Authorization": "Bearer ..."}`` dict for the target
    audience, cached per-audience. Returns ``{}`` when no credentials
    are available (local dev / tests) or on a transient fetch failure.

    The cached dict is returned by identity per audience; httpx merges
    headers without mutation so this is safe. Transient failures are
    held for 30 s only — the next request after cooldown retries."""
    cached = _cache.get(audience)
    if cached is not None:
        return cached
    if audience in _failure_cache:
        # Recent fetch failure — return empty header without retrying
        # so we don't hammer the metadata server during an incident.
        return {}

    async with _lock_for(audience):
        cached = _cache.get(audience)
        if cached is not None:
            return cached
        if audience in _failure_cache:
            return {}
        try:
            result = await asyncio.to_thread(_fetch_blocking, audience)
        except Exception as exc:
            # Network errors (metadata server slow / unreachable) deserve
            # WARNING so Cloud Logging exporters pick up the root cause
            # instead of operators chasing a 401 flood downstream. BUT
            # ``DefaultCredentialsError`` fires on every dev laptop that
            # hasn't run ``gcloud auth application-default login`` — that
            # class is DEBUG so the WARNING channel stays signal-rich.
            if _is_default_credentials_error(exc):
                logger.debug("No ADC configured, skipping ID token for %s: %s", audience, exc)
            else:
                logger.warning("ID token fetch failed for %s: %s", audience, exc)
            _failure_cache[audience] = True
            return {}

        if isinstance(result, _NoCredentials):
            # Permanent — cache empty header at the full TTL.
            _cache[audience] = {}
            return {}

        header: dict[str, str] = {"Authorization": f"Bearer {result}"}
        _cache[audience] = header
        return header


def _is_default_credentials_error(exc: BaseException) -> bool:
    """True if ``exc`` is google.auth's ``DefaultCredentialsError``
    (raised on dev laptops without ADC). Imported lazily so the module
    still works when google.auth isn't installed at all."""
    try:
        from google.auth.exceptions import (  # type: ignore[import-untyped]
            DefaultCredentialsError,
        )
    except ImportError:
        return False
    return isinstance(exc, DefaultCredentialsError)


def evict(audience: str) -> None:
    """Drop cached entries for ``audience``. Storage client calls this
    on 401 responses so a mid-TTL credential rotation or misconfig fix
    self-heals on the next request instead of waiting 50 min."""
    _cache.pop(audience, None)
    _failure_cache.pop(audience, None)
