"""Cloud Run ID token for core-operations' calls to core-api.

When core-api runs on Cloud Run with ``--no-allow-unauthenticated``, every
caller must present an ID token whose audience is core-api's URL. On Cloud
Run the metadata server issues one to the service's own account, and
``google.auth`` wraps the fetch. The same pattern as
``core_api.clients.identity_token``, with two differences:

- The token goes in ``X-Serverless-Authorization``. Cloud Run checks it and
  strips its signature, and it leaves ``X-API-Key`` (the admin credential)
  and ``Authorization`` to core-api.
- Only an ``https://`` core-api URL gets a token. A compose or on-prem
  core-api on ``http://`` sits on the internal network, has no Cloud Run
  front end, and has no metadata server to ask.

A token that cannot be fetched is not fatal: the call goes out without one,
core-api answers 401 or 403, and the tick logs that as it logs any non-2xx.
The failure is held for 30 s so a metadata outage is not retried on every
call.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from urllib.parse import urlsplit

import httpx
from cachetools import TTLCache  # type: ignore[import-untyped]

from core_operations.config import settings

logger = logging.getLogger(__name__)

HEADER = "X-Serverless-Authorization"

# The metadata server answers in well under a second; 5 s bounds a hung
# fetch so it cannot hold the audience lock for the length of a tick.
_METADATA_SERVER_TIMEOUT_SECONDS = 5.0

# Google ID tokens last an hour; 50 minutes keeps a sent token at least 10
# minutes from expiry. core-operations has one audience, core-api.
_cache: TTLCache[str, str] = TTLCache(maxsize=4, ttl=50 * 60)
_failure_cache: TTLCache[str, bool] = TTLCache(maxsize=4, ttl=30)
_audience_locks: dict[str, asyncio.Lock] = {}


def _fetch_blocking(audience: str) -> str:
    import requests  # type: ignore[import-untyped]
    from google.auth.transport.requests import Request  # type: ignore[import-untyped]
    from google.oauth2 import id_token  # type: ignore[import-untyped]

    # A Session subclass forces the timeout; google.auth passes its own
    # ``timeout`` keyword, so a partial would collide with it.
    class _TimeoutSession(requests.Session):  # type: ignore[misc]
        def request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            kwargs["timeout"] = _METADATA_SERVER_TIMEOUT_SECONDS
            return super().request(*args, **kwargs)

    with _TimeoutSession() as session:
        return str(id_token.fetch_id_token(Request(session=session), audience))


async def fetch_token(audience: str) -> str | None:
    """Return a cached ID token for ``audience``, or None if none can be had."""
    cached = _cache.get(audience)
    if cached is not None:
        return str(cached)
    if audience in _failure_cache:
        return None
    lock = _audience_locks.get(audience)
    if lock is None:
        lock = _audience_locks[audience] = asyncio.Lock()
    async with lock:
        cached = _cache.get(audience)
        if cached is not None:
            return str(cached)
        if audience in _failure_cache:
            return None
        try:
            token = await asyncio.to_thread(_fetch_blocking, audience)
        except Exception as exc:
            logger.warning("ID token fetch for %s failed: %s", audience, exc)
            _failure_cache[audience] = True
            return None
        _cache[audience] = token
        return token


def evict(audience: str) -> None:
    """Forget ``audience``'s token so the next call fetches a fresh one."""
    _cache.pop(audience, None)
    _failure_cache.pop(audience, None)


class CoreApiIdentity(httpx.Auth):
    """Adds core-api's ID token to each request; drops it after a 401."""

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        # Cloud Run matches the audience against the service URL, which is an
        # origin: scheme and host, never a path.
        url = urlsplit(settings.core_api_url)
        if url.scheme != "https":
            yield request
            return
        audience = f"https://{url.netloc}"
        token = await fetch_token(audience)
        if token:
            request.headers[HEADER] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            # Not retried: the tick runs again on its next schedule, and a
            # retried POST could repeat work core-api has already started.
            evict(audience)
