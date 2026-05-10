"""Regression test for CAURA-644: query-embedding cache key must include
``VECTOR_DIM`` so a schema-dim migration doesn't leak stale-dim cached
embeddings into the new schema.

Surfaced during the v2.0.0 prod rollout (CAURA-641, 2026-05-04): migration
012 widened the pgvector schema from 768 → 1024 dimensions, but the
``qemb2:*`` cache key only included {model, tenant_id, query_text} —
not the dimension. Cached entries from before the migration were retrieved
by post-migration code as "1024-dim" but were actually 768, then handed
to a SQL similarity search → ``expected 1024 dimensions, not 768``.

The fix in ``memory_service._get_or_cache_embedding`` adds ``VECTOR_DIM``
to the hash input. This test locks in that the key changes when
``VECTOR_DIM`` changes.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from core_api.services import memory_service


@pytest.mark.asyncio
async def test_qemb_cache_key_changes_with_vector_dim(monkeypatch):
    """A schema-dim change must invalidate the cache by miss-routing the lookup.

    We invoke ``_get_or_cache_embedding`` twice with identical (query,
    tenant_id, tenant_config) but different ``VECTOR_DIM`` values, and
    capture the cache keys. They must differ — otherwise a 768→1024
    migration like CAURA-444 would silently serve the old-dim entry.
    """
    captured_keys: list[str] = []

    async def _capture_get(key: str) -> str | None:
        captured_keys.append(key)
        return None  # always miss → triggers fresh embed

    async def _noop_set(key: str, value: str, ttl: int = 0) -> None:
        return None

    fake_embedding_768 = [0.1] * 768
    fake_embedding_1024 = [0.1] * 1024

    # First call: VECTOR_DIM=768, expect a key derived from that dim.
    with (
        monkeypatch.context() as m,
        patch("core_api.cache.cache_get", new=_capture_get),
        patch("core_api.cache.cache_set", new=_noop_set),
        patch.object(
            memory_service,
            "get_query_embedding",
            new=AsyncMock(return_value=fake_embedding_768),
        ),
    ):
        m.setattr(memory_service, "VECTOR_DIM", 768)
        result = await memory_service._get_or_cache_embedding(
            query="hello world",
            tenant_id="t1",
            tenant_config=None,
        )
        assert result == fake_embedding_768

    # Second call: same args but VECTOR_DIM=1024 — must produce a
    # different cache key, otherwise the post-migration path would serve
    # the stale 768-dim entry.
    with (
        monkeypatch.context() as m,
        patch("core_api.cache.cache_get", new=_capture_get),
        patch("core_api.cache.cache_set", new=_noop_set),
        patch.object(
            memory_service,
            "get_query_embedding",
            new=AsyncMock(return_value=fake_embedding_1024),
        ),
    ):
        m.setattr(memory_service, "VECTOR_DIM", 1024)
        result = await memory_service._get_or_cache_embedding(
            query="hello world",
            tenant_id="t1",
            tenant_config=None,
        )
        assert result == fake_embedding_1024

    assert len(captured_keys) == 2, "expected one cache_get per call"
    key_768, key_1024 = captured_keys
    assert key_768 != key_1024, (
        "Cache key must include VECTOR_DIM so a 768→1024 migration "
        "(CAURA-444) doesn't surface stale-dim cached embeddings to the "
        "new schema (CAURA-644). Got the same key for both dims:\n"
        f"  768  → {key_768}\n  1024 → {key_1024}"
    )


@pytest.mark.asyncio
async def test_qemb_cache_hit_returns_cached_value(monkeypatch):
    """Sanity: when the cache key matches, the cached value is returned
    without a fresh embed call. Locks in that the new key shape doesn't
    accidentally always-miss.
    """
    expected_dim = 1024
    cached_embedding = [0.5] * expected_dim
    cached_payload = json.dumps(cached_embedding)

    async def _hit_get(key: str) -> str:
        return cached_payload

    async def _noop_set(key: str, value: str, ttl: int = 0) -> None:
        return None

    embed_mock = AsyncMock(return_value=[0.0] * expected_dim)

    with (
        monkeypatch.context() as m,
        patch("core_api.cache.cache_get", new=_hit_get),
        patch("core_api.cache.cache_set", new=_noop_set),
        patch.object(memory_service, "get_query_embedding", new=embed_mock),
    ):
        m.setattr(memory_service, "VECTOR_DIM", expected_dim)
        result = await memory_service._get_or_cache_embedding(
            query="anything",
            tenant_id="t1",
            tenant_config=None,
        )

    assert result == cached_embedding
    embed_mock.assert_not_awaited()
