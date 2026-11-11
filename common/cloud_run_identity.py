"""Cloud Run identity tokens for self-hosted model backends.

A TEI or rerank service on Cloud Run without public invocation needs each
caller to present an ID token whose audience is the service's URL. The
operator opts each such backend in by listing its origin in
``CLOUD_RUN_ID_TOKEN_ORIGINS`` (comma- or space-separated; a pasted URL with
a path or trailing slash is reduced to its origin). A request to a listed
origin then carries a token from the metadata server in
``X-Serverless-Authorization``, which Cloud Run checks and which leaves the
backend's own ``Authorization`` header (the OpenAI-style key, the reranker's
bearer) alone. Every other request is unchanged, and with the variable unset
nothing here runs.

The audience is the origin, never the request path: Cloud Run matches it
against the service URL. Tokens are cached for 50 of their 60 minutes; a
failed fetch is held for 30 s and the request goes out without a token, so
Cloud Run answers 403 and the caller's existing error handling reports it.
A 401 drops the cached token so the next request fetches a new one.

The providers wrap this in their own ``Auth`` classes because they use two
different httpx packages (``httpx`` and the OpenAI SDK's ``httpx2``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ORIGINS_ENV = "CLOUD_RUN_ID_TOKEN_ORIGINS"
HEADER = "X-Serverless-Authorization"

# The metadata server answers in well under a second; 5 s bounds a hung
# fetch so it cannot hold the audience lock for the length of a request.
_METADATA_SERVER_TIMEOUT_SECONDS = 5.0

_TOKEN_TTL_SECONDS = 50 * 60
_FAILURE_HOLD_SECONDS = 30.0

# audience -> (token, monotonic expiry) and audience -> monotonic expiry.
# Plain dicts rather than cachetools: core-storage-api's backfill imports
# the embedder and does not install it. Audiences come only from the
# operator's list, so these stay small.
_cache: dict[str, tuple[str, float]] = {}
_failure_cache: dict[str, float] = {}
_audience_locks: dict[str, asyncio.Lock] = {}


def _cached(audience: str) -> str | None:
    entry = _cache.get(audience)
    if entry is None or entry[1] <= time.monotonic():
        return None
    return entry[0]


def _held(audience: str) -> bool:
    return _failure_cache.get(audience, 0.0) > time.monotonic()


def origin_of(url: str) -> str:
    """``scheme://host[:port]`` of ``url``, lower-cased; "" if it has none."""
    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}".lower()


def listed_origins() -> frozenset[str]:
    raw = os.environ.get(ORIGINS_ENV, "")
    return frozenset(
        o for o in (origin_of(u) for u in raw.replace(",", " ").split()) if o
    )


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


async def _fetch_token(audience: str) -> str | None:
    cached = _cached(audience)
    if cached is not None:
        return cached
    if _held(audience):
        return None
    lock = _audience_locks.get(audience)
    if lock is None:
        lock = _audience_locks[audience] = asyncio.Lock()
    async with lock:
        cached = _cached(audience)
        if cached is not None:
            return cached
        if _held(audience):
            return None
        try:
            token = await asyncio.to_thread(_fetch_blocking, audience)
        except ImportError as exc:
            logger.error(
                "%s lists %s, but google-auth is not installed, so no ID "
                "token can be fetched: %s",
                ORIGINS_ENV,
                audience,
                exc,
            )
            _failure_cache[audience] = time.monotonic() + _FAILURE_HOLD_SECONDS
            return None
        except Exception as exc:
            logger.warning("ID token fetch for %s failed: %s", audience, exc)
            _failure_cache[audience] = time.monotonic() + _FAILURE_HOLD_SECONDS
            return None
        _cache[audience] = (token, time.monotonic() + _TOKEN_TTL_SECONDS)
        return token


async def headers_for(url: str) -> dict[str, str]:
    """The identity header for a request to ``url``, or {} if it gets none."""
    audience = origin_of(url)
    if not audience or audience not in listed_origins():
        return {}
    token = await _fetch_token(audience)
    return {HEADER: f"Bearer {token}"} if token else {}


def refused(url: str, status_code: int) -> None:
    """Drop ``url``'s cached token after Cloud Run answered 401 for it."""
    if status_code == 401:
        audience = origin_of(url)
        _cache.pop(audience, None)
        _failure_cache.pop(audience, None)
