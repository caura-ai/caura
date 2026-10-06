"""A real embedding provider with no credential must not persist hash vectors.

``EMBEDDING_PROVIDER=openai`` is the default. With no tenant key, no
``OPENAI_API_KEY`` and no platform embedder, the registry fell back to the fake
provider with only a WARNING, and every write stored its hash vector as the
row's embedding. Nothing marks such a row, so once a key is added it stays
invisible to semantic search and no backfill can select it (the backfill only
picks ``embedding IS NULL``).

Now the persisting entry points answer ``None`` — the row is stored unembedded,
the state the FTS admission guard and the backfill already handle — while the
query path keeps working so a keyless deployment can still search by keyword.
``EMBEDDING_PROVIDER=fake`` (the explicit opt-in) is unchanged.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

import common.embedding._platform as platform_mod
from common.constants import VECTOR_DIM
from common.embedding import (
    FakeEmbeddingProvider,
    get_embedding,
    get_embedding_provider,
    get_embeddings_batch,
    get_query_embedding,
)
from tests.conftest import get_test_auth, uid


@pytest.fixture
def keyless_openai(monkeypatch):
    """The shipped default with no key anywhere."""
    from core_api import config
    from core_api.services import organization_settings as org

    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # Both bindings: ``ResolvedConfig`` reads the one organization_settings
    # imported, which another test module may have bound to a replacement.
    for s in {
        id(config.settings): config.settings,
        id(org.global_settings): org.global_settings,
    }.values():
        monkeypatch.setattr(s, "embedding_provider", "openai")
        monkeypatch.setattr(s, "openai_api_key", None)
    monkeypatch.setattr(platform_mod, "get_platform_embedding", lambda: None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_persisted_embedding_is_none_without_a_credential(keyless_openai):
    assert await get_embedding("our auth service uses JWT", background=False) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_persisted_batch_is_all_none_without_a_credential(keyless_openai):
    assert await get_embeddings_batch(["a", "b"], background=False) == [None, None]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_query_embedding_still_resolves_without_a_credential(keyless_openai):
    """A query vector is never stored; refusing it would 503 every keyless search."""
    vec = await get_query_embedding("JWT expiry")

    assert vec is not None and len(vec) == VECTOR_DIM


@pytest.mark.unit
def test_the_registry_still_returns_a_fake_compatible_provider(keyless_openai):
    provider = get_embedding_provider("openai", None)

    assert isinstance(provider, FakeEmbeddingProvider)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_explicit_fake_provider_still_embeds(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "fake")

    vec = await get_embedding("our auth service uses JWT", background=False)
    batch = await get_embeddings_batch(["a", "b"], background=False)

    assert vec is not None and len(vec) == VECTOR_DIM
    assert all(v is not None for v in batch)


@pytest.mark.asyncio
async def test_a_keyless_write_is_stored_unembedded(client, sc, keyless_openai):
    """The README quickstart write, on the default provider with no key. The
    row lands ``embedding=NULL``, which the search path's FTS admission guard
    already serves by keyword (``query`` is embedded with the stand-in above)."""
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"keyless-{uid()}",
        "write_mode": "strong",
        "content": f"Our auth service uses JWT with 15-minute expiry {uid()}.",
    }

    resp = await client.post("/api/v1/memories", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["metadata"]["embedding_pending"] is True

    row = await sc.get_memory(resp.json()["id"], tenant_id=tenant_id, read=False)
    assert row is not None
    assert row.get("embedding") is None, "a stand-in hash vector was persisted"


@pytest.mark.asyncio
async def test_a_keyless_bulk_write_is_stored_unembedded(client, sc, keyless_openai):
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "agent_id": f"keyless-bulk-{uid()}",
        "items": [
            {"content": f"keyless bulk alpha {uid()}", "write_mode": "strong"},
            {"content": f"keyless bulk beta {uid()}", "write_mode": "strong"},
        ],
    }

    resp = await client.post(
        "/api/v1/memories/bulk",
        json=body,
        headers={**headers, "X-Bulk-Attempt-Id": f"keyless-{uid()}"},
    )

    assert resp.status_code == 200, resp.text
    ids = [r["id"] for r in resp.json()["results"] if r.get("id")]
    assert len(ids) == 2
    for mid in ids:
        row = await sc.get_memory(mid, tenant_id=tenant_id, read=False)
        assert row.get("embedding") is None


@pytest.mark.asyncio
async def test_a_keyless_document_write_succeeds_unindexed(client, keyless_openai):
    tenant_id, headers = get_test_auth()
    body = {
        "tenant_id": tenant_id,
        "collection": "notes",
        "doc_id": f"keyless-{uid()}",
        "data": {"summary": "a document with a summary"},
    }

    resp = await client.post("/api/v1/documents", json=body, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["indexed"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_inline_re_embed_does_not_retry_an_unconfigured_provider(
    monkeypatch, keyless_openai
):
    """Retrying cannot produce a vector; the row is recorded unembedded once."""
    from unittest.mock import AsyncMock

    from core_api.services import memory_service

    stranded = AsyncMock()
    embed = AsyncMock(return_value=None)
    monkeypatch.setattr(memory_service, "_record_stranded", stranded)
    monkeypatch.setattr(memory_service, "get_embedding", embed)
    # Not inline, so the re-embed skips its initial backoff sleep.
    monkeypatch.setattr(memory_service.settings, "deployment_mode", "deferred")
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=None),
    )

    await memory_service._reembed_memory(uuid4(), "content", "t-keyless")

    embed.assert_not_awaited()
    stranded.assert_awaited_once()
    assert "no embedding provider is configured" in stranded.await_args.args[2]
