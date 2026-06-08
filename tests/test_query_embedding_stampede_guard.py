"""Concurrency tests for the query-embedding cache stampede guard (P4).

Wet-tested on memclaw.dev before the fix: 5 parallel recalls with a novel
query produced a 3-second tail spread (5.67s → 8.70s) — each request
independently issued its own ``get_query_embedding`` round-trip and
serialized on the embedding provider's HTTP client pool.

The fix in ``memory_service._get_or_cache_embedding`` registers a
process-local ``asyncio.Future`` per cache-key in ``_inflight_embeddings``
so concurrent cold-cache callers share a single embed round-trip.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from core_api.services import memory_service

pytestmark = [pytest.mark.unit]


_FAKE_EMBED = [0.1] * 8


@pytest.fixture(autouse=True)
def _clear_inflight():
    """Ensure inflight dict is empty across tests — otherwise a prior
    test that ended on an unresolved future would deadlock the next."""
    memory_service._inflight_embeddings.clear()
    yield
    memory_service._inflight_embeddings.clear()


async def test_concurrent_cold_misses_share_one_embed_call():
    """The smoking gun: N parallel callers, one upstream embed call."""
    embed_calls = 0

    async def _slow_embed(query, tenant_config):
        nonlocal embed_calls
        embed_calls += 1
        # Simulate the multi-second latency of a real OpenAI round-trip
        # so the stampede window is wide enough for all N callers to
        # arrive before the leader resolves its future.
        await asyncio.sleep(0.05)
        return _FAKE_EMBED

    async def _miss(_key):
        return None

    async def _noop_set(_key, _value, ttl=0):
        return None

    with (
        patch.object(memory_service, "get_query_embedding", new=_slow_embed),
        patch("core_api.cache.cache_get", new=_miss),
        patch("core_api.cache.cache_set", new=_noop_set),
    ):
        results = await asyncio.gather(
            *[
                memory_service._get_or_cache_embedding("novel query", "tenant-A", None)
                for _ in range(10)
            ]
        )

    assert embed_calls == 1, f"stampede: {embed_calls} upstream calls"
    # Every caller received the same embedding.
    for r in results:
        assert r == _FAKE_EMBED


async def test_different_queries_get_independent_futures():
    """Distinct cache keys must NOT share a future — otherwise queries
    would block each other on unrelated work."""
    embed_calls = 0

    async def _embed(query, tenant_config):
        nonlocal embed_calls
        embed_calls += 1
        await asyncio.sleep(0.02)
        return _FAKE_EMBED

    async def _miss(_key):
        return None

    async def _noop_set(_key, _value, ttl=0):
        return None

    with (
        patch.object(memory_service, "get_query_embedding", new=_embed),
        patch("core_api.cache.cache_get", new=_miss),
        patch("core_api.cache.cache_set", new=_noop_set),
    ):
        await asyncio.gather(
            memory_service._get_or_cache_embedding("query A", "tenant-A", None),
            memory_service._get_or_cache_embedding("query B", "tenant-A", None),
            memory_service._get_or_cache_embedding("query C", "tenant-A", None),
        )

    assert embed_calls == 3, "distinct queries should produce distinct embed calls"


async def test_cache_hit_skips_inflight_path_entirely():
    """A warm cache must short-circuit before the inflight check — no
    future registration, no contention with concurrent cold misses."""
    embed_calls = 0

    async def _embed(query, tenant_config):
        nonlocal embed_calls
        embed_calls += 1
        return _FAKE_EMBED

    cached_payload = "[0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1]"

    async def _hit(_key):
        return cached_payload

    async def _noop_set(_key, _value, ttl=0):
        return None

    with (
        patch.object(memory_service, "get_query_embedding", new=_embed),
        patch("core_api.cache.cache_get", new=_hit),
        patch("core_api.cache.cache_set", new=_noop_set),
    ):
        result = await memory_service._get_or_cache_embedding(
            "warm query", "tenant-A", None
        )

    assert embed_calls == 0
    assert result == _FAKE_EMBED
    # Inflight map stays empty when the lookup hits cache.
    assert memory_service._inflight_embeddings == {}


async def test_inflight_slot_cleared_on_success():
    """After a successful resolution the slot must be removed so a
    subsequent cold call with the same key starts a fresh future
    (rather than awaiting a stale completed one indefinitely held)."""

    async def _embed(query, tenant_config):
        return _FAKE_EMBED

    async def _miss(_key):
        return None

    async def _noop_set(_key, _value, ttl=0):
        return None

    with (
        patch.object(memory_service, "get_query_embedding", new=_embed),
        patch("core_api.cache.cache_get", new=_miss),
        patch("core_api.cache.cache_set", new=_noop_set),
    ):
        await memory_service._get_or_cache_embedding("q", "tenant-A", None)
        assert memory_service._inflight_embeddings == {}
        # A second call also clears.
        await memory_service._get_or_cache_embedding("q", "tenant-A", None)
        assert memory_service._inflight_embeddings == {}


async def test_exception_propagates_to_all_waiters():
    """If the leader's embed call raises, every waiter must see the
    same exception — otherwise N waiters would hang forever on an
    unresolved future."""
    leader_started = asyncio.Event()

    async def _failing_embed(query, tenant_config):
        leader_started.set()
        await asyncio.sleep(0.02)
        raise RuntimeError("upstream embed failed")

    async def _miss(_key):
        return None

    async def _noop_set(_key, _value, ttl=0):
        return None

    with (
        patch.object(memory_service, "get_query_embedding", new=_failing_embed),
        patch("core_api.cache.cache_get", new=_miss),
        patch("core_api.cache.cache_set", new=_noop_set),
    ):
        results = await asyncio.gather(
            *[
                memory_service._get_or_cache_embedding("doomed", "tenant-A", None)
                for _ in range(5)
            ],
            return_exceptions=True,
        )

    assert len(results) == 5
    # Every caller saw an exception. Leader raises RuntimeError directly;
    # waiters await the future and see the same RuntimeError it carries.
    for r in results:
        assert isinstance(r, RuntimeError)
        assert str(r) == "upstream embed failed"
    # And the inflight slot is cleared so the next call doesn't await a
    # rejected ghost future.
    assert memory_service._inflight_embeddings == {}


async def test_timeout_propagates_to_all_waiters():
    """get_query_embedding is wrapped in asyncio.wait_for(timeout=10).
    A timeout on the leader must also propagate."""

    async def _hangs(query, tenant_config):
        # The wait_for(timeout=10) wrapper inside _get_or_cache_embedding
        # is too slow for a unit test, so make our embed take longer
        # than that. We instead monkeypatch the wait_for timeout itself
        # for this case via a faster stub.
        await asyncio.sleep(2.0)
        return _FAKE_EMBED

    async def _miss(_key):
        return None

    async def _noop_set(_key, _value, ttl=0):
        return None

    # Patch wait_for to a 50ms timeout so the test completes quickly.
    real_wait_for = asyncio.wait_for

    async def _fast_wait_for(coro, timeout):
        return await real_wait_for(coro, 0.05)

    with (
        patch.object(memory_service, "get_query_embedding", new=_hangs),
        patch("core_api.cache.cache_get", new=_miss),
        patch("core_api.cache.cache_set", new=_noop_set),
        patch("core_api.services.memory_service.asyncio.wait_for", new=_fast_wait_for),
    ):
        results = await asyncio.gather(
            *[
                memory_service._get_or_cache_embedding("slow", "tenant-A", None)
                for _ in range(3)
            ],
            return_exceptions=True,
        )

    for r in results:
        assert isinstance(r, asyncio.TimeoutError)
    assert memory_service._inflight_embeddings == {}
