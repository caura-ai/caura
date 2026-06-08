"""Redis cache client with graceful fallback to in-memory."""

import json
import logging
from typing import Any

from redis.asyncio import from_url

from core_api.config import settings

logger = logging.getLogger(__name__)

_redis = None
_redis_available: bool | None = None  # None = not checked yet


async def _get_redis():
    """Lazy-init async Redis connection."""
    global _redis, _redis_available
    if _redis_available is False:
        return None
    if _redis is not None:
        return _redis
    if not settings.redis_url:
        _redis_available = False
        return None
    try:
        _redis = from_url(settings.redis_url, decode_responses=True)
        await _redis.ping()
        _redis_available = True
        logger.info("Redis connected: %s", settings.redis_url.split("@")[-1])
        return _redis
    except Exception as e:
        logger.warning("Redis unavailable, using in-memory fallback: %s", e)
        _redis_available = False
        _redis = None
        return None


# ── Simple key/value operations with TTL ──


async def cache_get(key: str) -> str | None:
    """Get a string value from Redis. Returns None on miss or if Redis unavailable."""
    r = await _get_redis()
    if r is None:
        return None
    try:
        return await r.get(key)
    except Exception:
        logger.debug("cache_get failed for key=%s", key, exc_info=True)
        return None


async def cache_set(key: str, value: str, ttl: int = 120) -> bool:
    """Set a string value in Redis with TTL (seconds). Returns True on success."""
    r = await _get_redis()
    if r is None:
        return False
    try:
        await r.set(key, value, ex=ttl)
        return True
    except Exception:
        logger.debug("cache_set failed for key=%s", key, exc_info=True)
        return False


async def cache_delete(key: str) -> bool:
    """Delete a key from Redis. Returns True on success."""
    r = await _get_redis()
    if r is None:
        return False
    try:
        await r.delete(key)
        return True
    except Exception:
        logger.debug("cache_delete failed for key=%s", key, exc_info=True)
        return False


async def cache_set_nx(key: str, value: str, ttl: int) -> bool:
    """Atomic SET-if-not-exists with TTL. Returns True iff we newly set
    the key (i.e. the caller "owns" the lock); False if the key already
    existed.

    **Fail-open semantics**: when Redis is unavailable (``_get_redis``
    returns ``None``) or the SET call raises, returns True. The
    idempotency check using this helper is best-effort — losing it
    means falling back to whatever upstream behaviour exists (e.g.
    storage CAS guards), never silently blocking work.
    """
    r = await _get_redis()
    if r is None:
        return True
    try:
        result = await r.set(key, value, ex=ttl, nx=True)
        return bool(result)
    except Exception:
        logger.debug("cache_set_nx failed for key=%s", key, exc_info=True)
        return True


# ── JSON helpers ──


async def cache_get_json(key: str) -> Any | None:
    """Get and deserialize a JSON value."""
    raw = await cache_get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


async def cache_set_json(key: str, value: Any, ttl: int = 120) -> bool:
    """Serialize and set a JSON value."""
    try:
        return await cache_set(key, json.dumps(value), ttl)
    except (TypeError, ValueError):
        return False


# ── Health check ──


async def redis_healthy() -> bool:
    """Live connectivity probe for the /health deploy gate.

    Bypasses `_get_redis()` so that a transient startup failure — which
    sticks `_redis_available=False` for the rest of the process life —
    doesn't wedge the health gate into a permanent 503. Always issues a
    fresh PING with a short socket timeout so the probe itself can't
    outlast the Cloud Run health-check budget.
    """
    if not settings.redis_url:
        # Caller guards on settings.redis_url; treat "not configured" as
        # healthy here so any future callsite can't accidentally 503.
        return True
    try:
        r = from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        try:
            return bool(await r.ping())
        finally:
            await r.aclose()
    except Exception:
        logger.warning("Redis ping failed", exc_info=True)
        return False
