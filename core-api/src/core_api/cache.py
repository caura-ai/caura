"""Redis cache client with graceful fallback to in-memory."""

import logging
import time

from redis.asyncio import from_url

from core_api.config import settings

logger = logging.getLogger(__name__)

_redis = None
# Monotonic deadline before which a failed connection is not retried. 0.0 means
# "no failure recorded", which is also how the first failure of a run is told
# from the ones after it for logging.
_redis_retry_after: float = 0.0

# How long to wait after a failed connection before trying again.
#
# The failure used to be PERMANENT: a ``_redis_available = False`` flag was
# never cleared, so one refused connection at first use — core-api racing Redis
# on a cold start is the ordinary way to get one — disabled Redis for the life
# of the process. Everything downstream then ran on its fallback until someone
# restarted the pod, with one WARNING line to say so.
#
# ``redis_healthy`` below is the evidence that this was wrong rather than
# clever: it bypasses this function entirely, and said so in its docstring,
# precisely so the health gate would not be wedged by the latch. The probe was
# made honest and every actual CONSUMER was left on it.
#
# SCOPE, because the name suggests more than it delivers: this covers the
# never-connected case, which is the one the audit filed. Once a connection has
# been established, ``_redis`` is returned before the check below and nothing
# marks it down when an individual COMMAND later fails — the pool reconnects per
# command instead. Covering that means having the command wrappers below record
# the failure, which is a wider change than this one and is not made here.
_RECONNECT_COOLDOWN_SECONDS = 30.0


async def _get_redis():
    """Lazy-init async Redis connection, retried after a cooldown.

    Returns None when Redis is not configured or not reachable; every caller
    here already handles that, and read paths degrade to their fallback. What
    changed is that "not reachable at startup" is no longer a permanent answer
    — see ``_RECONNECT_COOLDOWN_SECONDS``, including what it does not cover.

    Socket timeouts are deliberately left to redis-py, which defaults both
    ``socket_timeout`` and ``socket_connect_timeout`` to 5s (verified against
    the pinned 8.0.1). An earlier revision set them to 2s here on the grounds
    that a reconnect would otherwise block on the OS default; that was simply
    false, and the change would have narrowed the window for every consumer of
    this shared client — including ``cache_set_nx``, which fails OPEN, so a
    timeout there grants a lease rather than refusing one.
    """
    global _redis, _redis_retry_after
    if _redis is not None:
        return _redis
    if not settings.redis_url:
        # Not transient and not worth a cooldown: a URL absent from the
        # environment cannot appear without a restart. Left unlatched anyway so
        # a test (or a future runtime reload) setting it is not ignored forever
        # — the check is this cheap.
        return None
    if time.monotonic() < _redis_retry_after:
        return None
    try:
        _redis = from_url(settings.redis_url, decode_responses=True)
        await _redis.ping()
        _redis_retry_after = 0.0
        logger.info("Redis connected: %s", settings.redis_url.split("@")[-1])
        return _redis
    except Exception as e:
        # WARNING on the first failure of a run, DEBUG while the cooldown keeps
        # re-failing, so a long outage does not write one identical line per
        # interval for its whole duration.
        if _redis_retry_after == 0.0:
            logger.warning("Redis unavailable, using in-memory fallback: %s", e)
        else:
            logger.debug("Redis still unavailable: %s", e)
        _redis = None
        _redis_retry_after = time.monotonic() + _RECONNECT_COOLDOWN_SECONDS
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


# Release-if-owner. GET-then-DEL from the client would be two round trips with a
# window in between, which is the whole bug this guards against; the compare and
# the delete have to be one atomic step, so they run server-side.
_DELETE_IF_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


async def cache_delete_if(key: str, expected: str) -> bool:
    """Delete ``key`` only while it still holds ``expected``. Returns True iff
    this call deleted it.

    For releasing a ``cache_set_nx`` lock. An unconditional ``DEL`` is the
    classic distributed-lock footgun: a holder whose work outruns the lock's
    TTL no longer owns the key, and deleting it then destroys a SUCCESSOR's
    lock rather than its own. Pairing SET NX with a per-acquisition token and
    releasing through this compare-and-delete makes a release a no-op once the
    lock has moved on.

    Same fail-quiet contract as its neighbours: no Redis, or a failed EVAL,
    returns False rather than raising. Callers release inside ``finally``
    blocks on paths whose never-raises property is load-bearing.
    """
    r = await _get_redis()
    if r is None:
        return False
    try:
        return bool(await r.eval(_DELETE_IF_SCRIPT, 1, key, expected))
    except Exception:
        logger.debug("cache_delete_if failed for key=%s", key, exc_info=True)
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


# ── Health check ──


async def redis_healthy() -> bool:
    """Live connectivity probe for the /health deploy gate.

    Bypasses `_get_redis()` and always issues a fresh PING with a short
    socket timeout, so the probe cannot outlast the Cloud Run health-check
    budget and cannot report a cached verdict.

    The bypass still matters, for a reason the permanent latch used to
    overshadow: inside the reconnect cooldown `_get_redis` returns None without
    probing, so a gate routed through it would report unhealthy for up to
    `_RECONNECT_COOLDOWN_SECONDS` AFTER Redis came back. The probe has to be
    able to see a recovery the cooldown is deliberately not looking for.
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
