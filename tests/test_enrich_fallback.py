import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
from core_api.services import memory_service


@pytest.mark.asyncio
async def test_enrich_scheduling_publishes_on_success(monkeypatch):
    """When inline_enrichment is False and publish succeeds, it should not fallback inline."""
    # Mock settings
    mock_settings = MagicMock()
    mock_settings.inline_enrichment = False
    monkeypatch.setattr(memory_service, "settings", mock_settings)

    mock_pub = AsyncMock()
    mock_inline = AsyncMock()

    # Patch our publisher and background worker
    monkeypatch.setattr(memory_service, "publish_memory_enrich_request", mock_pub)
    monkeypatch.setattr(memory_service, "_enrich_memory_background", mock_inline)

    memory_id = uuid4()
    await memory_service._schedule_enrich_or_inline(
        memory_id=memory_id,
        content="test content",
        tenant_id="test-tenant",
        fleet_id="test-fleet",
        agent_id="test-agent",
        tenant_config=None,
    )

    # Asserts
    mock_pub.assert_called_once_with(
        memory_id=memory_id,
        content="test content",
        tenant_id="test-tenant",
        tenant_config=None,
        reference_datetime=None,
        agent_provided_fields=None,
    )
    mock_inline.assert_not_called()


@pytest.mark.asyncio
async def test_enrich_scheduling_falls_back_on_publish_failure(monkeypatch):
    """When inline_enrichment is False and publish raises an exception, it should fallback to inline."""
    # Mock settings
    mock_settings = MagicMock()
    mock_settings.inline_enrichment = False
    monkeypatch.setattr(memory_service, "settings", mock_settings)

    # Simulate publisher failure (e.g. Redis connection error)
    mock_pub = AsyncMock(side_effect=Exception("Redis connection refused"))
    mock_inline = AsyncMock()

    # Patch our publisher and background worker
    monkeypatch.setattr(memory_service, "publish_memory_enrich_request", mock_pub)
    monkeypatch.setattr(memory_service, "_enrich_memory_background", mock_inline)

    memory_id = uuid4()
    await memory_service._schedule_enrich_or_inline(
        memory_id=memory_id,
        content="test content",
        tenant_id="test-tenant",
        fleet_id="test-fleet",
        agent_id="test-agent",
        tenant_config=None,
    )

    # Asserts
    mock_pub.assert_called_once()
    mock_inline.assert_called_once_with(
        memory_id, "test content", "test-tenant", "test-fleet", "test-agent"
    )
