"""C9 — embedding cache key includes ``EMBEDDING_QUERY_INSTRUCTION``.

For instruction-aware models (Qwen3-Embedding, e5-instruct, KaLM), the
same raw query is encoded differently depending on the resolved
task-description prefix the provider prepends. Before C9, the search
cache key was ``{model}:{VECTOR_DIM}:{tenant_id}:{normalized_query}`` —
no instruction. Switching ``EMBEDDING_QUERY_INSTRUCTION`` (set / unset /
edit) would serve embeddings from the prior instruction until the TTL
expired, looking like a model regression but actually a cache-key
omission.

This test file proves the new key shape is honored:
- Same query + different instruction → different cache key (no stale
  hit).
- Same query + same instruction → cache hit (steady-state unchanged).
- The cache prefix is bumped (``qemb4:``) so post-deploy Redis stats
  show a clean cold-start boundary.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from core_api.services import memory_service

pytestmark = [pytest.mark.unit]


_FAKE_EMBED_A = [0.1] * 8
_FAKE_EMBED_B = [0.2] * 8


@pytest.fixture(autouse=True)
def _clear_inflight():
    """Mirror the stampede-guard test setup so a leftover future from a
    prior test doesn't deadlock the next."""
    memory_service._inflight_embeddings.clear()
    yield
    memory_service._inflight_embeddings.clear()


async def test_cache_key_uses_qemb4_prefix(monkeypatch):
    """Sanity check on the prefix bump — proves the migration boundary
    so operators can grep ``qemb4:*`` in Redis stats post-deploy."""
    seen_keys: list[str] = []

    async def _capture_get(key):
        seen_keys.append(key)
        return None

    async def _noop_set(key, value, ttl=0):
        return None

    async def _embed(query, tenant_config):
        return _FAKE_EMBED_A

    monkeypatch.delenv("EMBEDDING_QUERY_INSTRUCTION", raising=False)
    with (
        patch("core_api.cache.cache_get", new=_capture_get),
        patch("core_api.cache.cache_set", new=_noop_set),
        patch.object(memory_service, "get_query_embedding", new=_embed),
    ):
        await memory_service._get_or_cache_embedding("q", "tenant-A", None)

    assert seen_keys, "cache_get was never called"
    assert seen_keys[0].startswith("qemb4:"), (
        f"expected qemb4: prefix, got {seen_keys[0]!r}"
    )


async def test_different_instruction_produces_different_cache_key(monkeypatch):
    """The point of C9: env-var change MUST flip the cache key so old
    embeddings (computed under the prior instruction) don't get served."""
    seen_keys: set[str] = set()

    async def _capture_get(key):
        seen_keys.add(key)
        return None

    async def _noop_set(key, value, ttl=0):
        return None

    async def _embed(query, tenant_config):
        return _FAKE_EMBED_A

    with (
        patch("core_api.cache.cache_get", new=_capture_get),
        patch("core_api.cache.cache_set", new=_noop_set),
        patch.object(memory_service, "get_query_embedding", new=_embed),
    ):
        monkeypatch.setenv("EMBEDDING_QUERY_INSTRUCTION", "Retrieve relevant docs")
        await memory_service._get_or_cache_embedding("q", "tenant-A", None)
        memory_service._inflight_embeddings.clear()

        monkeypatch.setenv(
            "EMBEDDING_QUERY_INSTRUCTION", "Find passages that answer the question"
        )
        await memory_service._get_or_cache_embedding("q", "tenant-A", None)

    assert len(seen_keys) == 2, f"expected two distinct keys, got {seen_keys}"


async def test_same_instruction_hits_cache(monkeypatch):
    """Steady-state: same query + same instruction → one embed call."""
    embed_calls = 0
    stored_value: dict[str, str] = {}

    async def _embed(query, tenant_config):
        nonlocal embed_calls
        embed_calls += 1
        return _FAKE_EMBED_A

    async def _cache_get(key):
        return stored_value.get(key)

    async def _cache_set(key, value, ttl=0):
        stored_value[key] = value

    monkeypatch.setenv("EMBEDDING_QUERY_INSTRUCTION", "Retrieve relevant docs")
    with (
        patch.object(memory_service, "get_query_embedding", new=_embed),
        patch("core_api.cache.cache_get", new=_cache_get),
        patch("core_api.cache.cache_set", new=_cache_set),
    ):
        # First call — cold cache, embeds once + writes.
        await memory_service._get_or_cache_embedding("q", "tenant-A", None)
        memory_service._inflight_embeddings.clear()
        # Second call — warm cache hit, no new embed.
        await memory_service._get_or_cache_embedding("q", "tenant-A", None)

    assert embed_calls == 1, f"expected 1 embed call, got {embed_calls}"


async def test_unset_vs_empty_instruction_equivalent(monkeypatch):
    """Unset and explicit-empty resolve to the same key — both fall
    through to the provider's no-prefix branch."""
    seen_keys: set[str] = set()

    async def _capture_get(key):
        seen_keys.add(key)
        return None

    async def _noop_set(key, value, ttl=0):
        return None

    async def _embed(query, tenant_config):
        return _FAKE_EMBED_A

    with (
        patch("core_api.cache.cache_get", new=_capture_get),
        patch("core_api.cache.cache_set", new=_noop_set),
        patch.object(memory_service, "get_query_embedding", new=_embed),
    ):
        monkeypatch.delenv("EMBEDDING_QUERY_INSTRUCTION", raising=False)
        await memory_service._get_or_cache_embedding("q", "tenant-A", None)
        memory_service._inflight_embeddings.clear()

        monkeypatch.setenv("EMBEDDING_QUERY_INSTRUCTION", "")
        await memory_service._get_or_cache_embedding("q", "tenant-A", None)

    assert len(seen_keys) == 1, (
        f"unset and empty should hash to the same key, got {seen_keys}"
    )
