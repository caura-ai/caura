"""Tests for handle_embed_request: payload validation, cache, provider, PATCH."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest

import core_worker.consumer as consumer
from common.events.base import Event
from core_worker.config import Settings

# ── Helpers ──────────────────────────────────────────────────────────


def _make_event(payload: dict | None = None) -> Event:
    return Event(
        event_type="memclaw.memory.embed-requested",
        tenant_id=(payload or {}).get("tenant_id"),
        payload=payload
        or {
            "memory_id": str(uuid4()),
            "tenant_id": "tenant-A",
            "content": "hello world",
        },
    )


@pytest.fixture
def settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


@pytest.fixture
def mock_provider(monkeypatch):
    """Replace the platform embedding singleton with a deterministic mock."""
    provider = MagicMock()
    provider.provider_name = "fake-platform"
    provider.model = "test-model"
    provider.embed = AsyncMock(return_value=[0.1] * 768)
    monkeypatch.setattr("common.embedding._platform._platform_embedding", provider)
    return provider


@pytest.fixture
def mock_storage_client():
    """Zero-arg factory returning a MagicMock that imitates httpx.AsyncClient.

    The consumer talks to ``find_embedding_by_content_hash`` and
    ``update_memory_embedding`` — both module-level helpers that take
    the client as their first arg. We patch those rather than mock the
    HTTP responses, because the helpers' job (httpx.get / patch) is
    not what's under test here.
    """
    client = MagicMock(spec=httpx.AsyncClient)
    return lambda: client


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_calls_provider_and_patches(
    monkeypatch, settings, mock_provider, mock_storage_client
):
    """Cache miss → provider call → PATCH succeeds."""
    consumer.configure(mock_storage_client)

    cache_lookup = AsyncMock(return_value=None)
    patch_call = AsyncMock(return_value=None)
    monkeypatch.setattr(consumer, "find_embedding_by_content_hash", cache_lookup)
    monkeypatch.setattr(consumer, "update_memory_embedding", patch_call)

    memory_id = uuid4()
    event = _make_event(
        {
            "memory_id": str(memory_id),
            "tenant_id": "tenant-A",
            "content": "hello async world",
            "content_hash": "h1",
        }
    )

    await consumer.handle_embed_request(event)

    cache_lookup.assert_awaited_once()
    mock_provider.embed.assert_awaited_once_with("hello async world")
    patch_call.assert_awaited_once()
    # Pull the kwargs from the PATCH call to check we passed the right shape.
    call_kwargs = patch_call.await_args.kwargs
    assert call_kwargs["memory_id"] == memory_id
    assert call_kwargs["tenant_id"] == "tenant-A"
    assert call_kwargs["embedding"] == [0.1] * 768


@pytest.mark.asyncio
async def test_cache_hit_skips_provider_call(monkeypatch, settings, mock_provider, mock_storage_client):
    """When the storage cache returns an embedding, we don't hit the provider."""
    consumer.configure(mock_storage_client)

    cached = [0.7] * 768
    cache_lookup = AsyncMock(return_value=cached)
    patch_call = AsyncMock(return_value=None)
    monkeypatch.setattr(consumer, "find_embedding_by_content_hash", cache_lookup)
    monkeypatch.setattr(consumer, "update_memory_embedding", patch_call)

    event = _make_event(
        {
            "memory_id": str(uuid4()),
            "tenant_id": "tenant-A",
            "content": "any text",
            "content_hash": "cached-hash",
        }
    )

    await consumer.handle_embed_request(event)

    mock_provider.embed.assert_not_called()
    patch_call.assert_awaited_once()
    assert patch_call.await_args.kwargs["embedding"] == cached


@pytest.mark.asyncio
async def test_no_content_hash_skips_cache_lookup(monkeypatch, settings, mock_provider, mock_storage_client):
    """Events without content_hash go straight to the provider — the GET is skipped."""
    consumer.configure(mock_storage_client)

    cache_lookup = AsyncMock(return_value=None)
    patch_call = AsyncMock(return_value=None)
    monkeypatch.setattr(consumer, "find_embedding_by_content_hash", cache_lookup)
    monkeypatch.setattr(consumer, "update_memory_embedding", patch_call)

    event = _make_event(
        {
            "memory_id": str(uuid4()),
            "tenant_id": "tenant-A",
            "content": "no hash",
        }
    )

    await consumer.handle_embed_request(event)

    cache_lookup.assert_not_called()
    mock_provider.embed.assert_awaited_once()
    patch_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_validation_error_drops_silently(settings, mock_provider, mock_storage_client, caplog):
    """A payload missing required fields must not raise."""
    consumer.configure(mock_storage_client)

    # Missing content + memory_id + tenant_id.
    event = Event(event_type="memclaw.memory.embed-requested", payload={})

    with caplog.at_level("ERROR"):
        await consumer.handle_embed_request(event)

    mock_provider.embed.assert_not_called()
    assert any(getattr(rec, "dropped", False) is True for rec in caplog.records), (
        "expected an alert-hook log record (dropped=True)"
    )


@pytest.mark.asyncio
async def test_no_platform_singleton_drops(monkeypatch, settings, mock_storage_client, caplog):
    """When PLATFORM_EMBEDDING_* is unset, the consumer drops with a loud log."""
    consumer.configure(mock_storage_client)
    monkeypatch.setattr("common.embedding._platform._platform_embedding", None)

    event = _make_event()
    with caplog.at_level("ERROR"):
        await consumer.handle_embed_request(event)

    assert any(getattr(rec, "dropped", False) is True for rec in caplog.records), (
        "expected dropped=True log when no platform singleton"
    )


@pytest.mark.asyncio
async def test_provider_error_propagates(monkeypatch, settings, mock_provider, mock_storage_client):
    """Provider exceptions must propagate so Pub/Sub redelivers."""
    consumer.configure(mock_storage_client)
    mock_provider.embed.side_effect = RuntimeError("provider down")
    cache_lookup = AsyncMock(return_value=None)
    patch_call = AsyncMock(return_value=None)
    monkeypatch.setattr(consumer, "find_embedding_by_content_hash", cache_lookup)
    monkeypatch.setattr(consumer, "update_memory_embedding", patch_call)

    event = _make_event()
    with pytest.raises(RuntimeError, match="provider down"):
        await consumer.handle_embed_request(event)

    patch_call.assert_not_called()


@pytest.mark.asyncio
async def test_update_memory_embedding_clears_embedding_pending_flag():
    """The hot-path writer set ``metadata.embedding_pending=true`` when
    deferring; the worker's PATCH must clear it via ``metadata_patch``
    on the same call so a read-after-success returns clean state.
    Storage's JSONB ``||`` merge overwrites the prior ``True``."""
    from core_worker.clients.storage_client import update_memory_embedding

    response = MagicMock(status_code=200)
    response.raise_for_status = MagicMock(return_value=None)
    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(return_value=response)

    await update_memory_embedding(
        client,
        memory_id=uuid4(),
        tenant_id="tenant-A",
        embedding=[0.1] * 768,
    )

    body = client.patch.await_args.kwargs["json"]
    assert body["embedding"] == [0.1] * 768
    assert body["tenant_id"] == "tenant-A"
    assert body["metadata_patch"] == {"embedding_pending": False}


@pytest.mark.asyncio
async def test_storage_404_acks_silently(caplog):
    """A 404 from storage (row deleted) ack-drops; doesn't redeliver."""
    from core_worker.clients.storage_client import update_memory_embedding

    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(return_value=MagicMock(status_code=404))

    with caplog.at_level("WARNING"):
        # Must NOT raise — the consumer would nack and Pub/Sub would
        # waste max-delivery-attempts on a deleted row.
        await update_memory_embedding(
            client,
            memory_id=uuid4(),
            tenant_id="tenant-A",
            embedding=[0.1] * 768,
        )

    assert any("not found in storage" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_storage_422_acks_silently(caplog):
    """A 422 from storage (schema rejection) ack-drops; doesn't redeliver."""
    from core_worker.clients.storage_client import update_memory_embedding

    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(return_value=MagicMock(status_code=422))

    with caplog.at_level("WARNING"):
        await update_memory_embedding(
            client,
            memory_id=uuid4(),
            tenant_id="tenant-A",
            embedding=[0.1] * 768,
        )

    assert any("422" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_storage_500_propagates():
    """A 5xx from storage MUST raise so the consumer nacks → redelivers."""
    from core_worker.clients.storage_client import update_memory_embedding

    response = MagicMock(status_code=500)
    response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("boom", request=MagicMock(), response=response)
    )
    client = MagicMock(spec=httpx.AsyncClient)
    client.patch = AsyncMock(return_value=response)

    with pytest.raises(httpx.HTTPStatusError):
        await update_memory_embedding(
            client,
            memory_id=uuid4(),
            tenant_id="tenant-A",
            embedding=[0.1] * 768,
        )


def test_register_consumers_subscribes_to_topic(monkeypatch):
    """register_consumers wires handle_embed_request to the right topic."""
    monkeypatch.setenv("EVENT_BUS_BACKEND", "inprocess")
    # Reset must run via asyncio since the new factory awaits stop()
    import asyncio

    from common.events.factory import get_event_bus, reset_event_bus_for_testing
    from common.events.topics import Topics

    asyncio.run(reset_event_bus_for_testing())

    consumer.register_consumers()
    bus = get_event_bus()
    assert Topics.Memory.EMBED_REQUESTED in bus._handlers
    assert consumer.handle_embed_request in bus._handlers[Topics.Memory.EMBED_REQUESTED]
