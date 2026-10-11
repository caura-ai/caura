"""EMBEDDED and ENRICHED announce a PATCH that landed, and only that (L-54).

Both events are defined as emitted "after successfully PATCHing". The PATCH
helpers ack-drop a 404 (row deleted) and a 422 (payload refused) without
raising, and returned None either way, so the worker announced a write storage
had refused. On a 422 core-api then read the row back, found no embedding, and
logged an invariant violation in the wrong service, naming the wrong cause.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

import core_worker.consumer as consumer
from common.enrichment import EnrichmentResult
from common.events.base import Event
from core_worker.clients.storage_client import (
    update_memory_embedding,
    update_memory_enrichment,
)


def _client(status_code: int) -> MagicMock:
    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(return_value=MagicMock(status_code=status_code))
    return client


@pytest.mark.parametrize(("status_code", "landed"), [(200, True), (404, False), (422, False)])
async def test_the_embedding_patch_says_whether_it_landed(status_code, landed) -> None:
    result = await update_memory_embedding(
        _client(status_code), memory_id=uuid4(), tenant_id="tenant-A", embedding=[0.1] * 4
    )

    assert result is landed


@pytest.mark.parametrize(("status_code", "landed"), [(200, True), (404, False), (422, False)])
async def test_the_enrichment_patch_says_whether_it_landed(status_code, landed) -> None:
    result = await update_memory_enrichment(
        _client(status_code), memory_id=uuid4(), tenant_id="tenant-A", fields={"title": "t"}
    )

    assert result is landed


async def test_an_empty_enrichment_has_nothing_to_land() -> None:
    """No fields means no PATCH, and nothing refused: the row is as enriched as
    this result can make it, so the event still goes out."""
    client = _client(200)

    result = await update_memory_enrichment(client, memory_id=uuid4(), tenant_id="tenant-A", fields={})

    assert result is True
    client.patch.assert_not_called()


async def test_a_refused_embedding_is_not_announced(monkeypatch) -> None:
    consumer.configure(lambda: MagicMock(spec=httpx.AsyncClient))
    provider = MagicMock(provider_name="fake-platform", model="test-model")
    provider.embed = AsyncMock(return_value=[0.1] * 768)
    monkeypatch.setattr("common.embedding._platform._platform_embedding", provider)
    monkeypatch.setattr(consumer, "find_embedding_by_content_hash", AsyncMock(return_value=None))
    monkeypatch.setattr(consumer, "update_memory_embedding", AsyncMock(return_value=False))
    publish = AsyncMock()
    monkeypatch.setattr(consumer, "publish_memory_embedded", publish)

    await consumer.handle_embed_request(
        Event(
            event_type="caura.memory.embed-requested",
            tenant_id="tenant-A",
            payload={"memory_id": str(uuid4()), "tenant_id": "tenant-A", "content": "x"},
        )
    )

    publish.assert_not_awaited()


async def test_a_refused_enrichment_is_not_announced(monkeypatch) -> None:
    consumer.configure(lambda: MagicMock(spec=httpx.AsyncClient))
    monkeypatch.setattr(
        consumer, "enrich_memory", AsyncMock(return_value=EnrichmentResult(memory_type="fact"))
    )
    monkeypatch.setattr(consumer, "update_memory_enrichment", AsyncMock(return_value=False))
    publish = AsyncMock()
    monkeypatch.setattr(consumer, "publish_memory_enriched", publish)

    await consumer.handle_enrich_request(
        Event(
            event_type="caura.memory.enrich-requested",
            tenant_id="tenant-A",
            payload={"memory_id": str(uuid4()), "tenant_id": "tenant-A", "content": "x"},
        )
    )

    publish.assert_not_awaited()
