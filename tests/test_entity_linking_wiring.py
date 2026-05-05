"""Integration wiring tests: entity-linking is triggered from lifecycle and extraction."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.services.lifecycle_service import run_lifecycle_for_tenant
from core_api.services.entity_extraction_worker import (
    process_entity_extraction,
)


# ── Helpers ───────────────────────────────────────────────────────────


def _fake_config(**overrides):
    """Return a mock ResolvedConfig with sensible defaults."""
    cfg = MagicMock()
    cfg.lifecycle_automation_enabled = True
    cfg.auto_crystallize_enabled = False
    cfg.auto_entity_linking_enabled = True
    cfg.entity_blocklist = frozenset()
    cfg.entity_extraction_provider = "openai"
    cfg.entity_extraction_model = "gpt-4o-mini"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


# ── lifecycle_service ────────────────────────────────────────────────


@pytest.mark.asyncio
@patch("core_api.services.lifecycle_service.get_storage_client")
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_lifecycle_runs_entity_linking_when_enabled(
    mock_resolve,
    mock_sc_factory,
):
    """run_lifecycle_for_tenant should run the entity-linking pipeline when enabled."""
    mock_resolve.return_value = _fake_config(auto_entity_linking_enabled=True)

    sc = AsyncMock()
    sc.archive_expired = AsyncMock(return_value=0)
    sc.archive_stale = AsyncMock(return_value=0)
    sc.count_active = AsyncMock(return_value=10)
    mock_sc_factory.return_value = sc

    mock_pipeline_result = MagicMock()
    mock_pipeline_result.step_count = 4

    mock_pipeline = MagicMock()
    mock_pipeline.run = AsyncMock(return_value=mock_pipeline_result)

    with (
        patch(
            "core_api.pipeline.compositions.entity_linking.build_full_entity_linking_pipeline",
            return_value=mock_pipeline,
        ),
        patch(
            "core_api.db.session.async_session",
        ) as mock_session_maker,
    ):
        mock_db = AsyncMock()
        mock_session_maker.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_session_maker.return_value.__aexit__ = AsyncMock(return_value=False)

        stats = await run_lifecycle_for_tenant("test-tenant")

    assert "entity_linking" in stats
    mock_pipeline.run.assert_awaited_once()


@pytest.mark.asyncio
@patch("core_api.services.lifecycle_service.get_storage_client")
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_lifecycle_skips_entity_linking_when_disabled(
    mock_resolve,
    mock_sc_factory,
):
    """Entity linking section should be skipped when auto_entity_linking_enabled=False."""
    mock_resolve.return_value = _fake_config(auto_entity_linking_enabled=False)

    sc = AsyncMock()
    sc.archive_expired = AsyncMock(return_value=0)
    sc.archive_stale = AsyncMock(return_value=0)
    mock_sc_factory.return_value = sc

    stats = await run_lifecycle_for_tenant("test-tenant")

    assert stats["entity_linking"] == {}


# ── entity_extraction_worker ─────────────────────────────────────────


@pytest.mark.asyncio
@patch(
    "core_api.services.entity_extraction_worker._discover_cross_links_for_memory",
    new_callable=AsyncMock,
)
@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.upsert_entity", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.get_embedding", new_callable=AsyncMock
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_extraction_triggers_cross_links_when_enabled(
    mock_resolve,
    mock_extract,
    mock_sc_factory,
    mock_embed,
    mock_upsert_entity,
    mock_upsert_relation,
    mock_log,
    mock_discover,
):
    """After entity extraction, cross-link discovery should be called when enabled."""
    mock_resolve.return_value = _fake_config(auto_entity_linking_enabled=True)

    # Mock graph result
    entity = MagicMock()
    entity.canonical_name = "Alice"
    entity.entity_type = "person"
    entity.role = "subject"
    graph = MagicMock()
    graph.entities = [entity]
    graph.relations = []
    mock_extract.return_value = graph

    sc = MagicMock()
    sc.find_entity_link = AsyncMock(return_value=None)
    sc.create_entity_link = AsyncMock()
    mock_sc_factory.return_value = sc

    mock_embed.return_value = [0.1] * 10

    upsert_result = MagicMock()
    upsert_result.id = uuid.uuid4()
    mock_upsert_entity.return_value = upsert_result

    memory_id = uuid.uuid4()

    with patch("core_api.tasks.track_task"):
        await process_entity_extraction(
            memory_id=memory_id,
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    mock_discover.assert_awaited_once_with(memory_id, "test-tenant", None)


@pytest.mark.asyncio
@patch(
    "core_api.services.entity_extraction_worker._discover_cross_links_for_memory",
    new_callable=AsyncMock,
)
@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.upsert_entity", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.get_embedding", new_callable=AsyncMock
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_extraction_skips_cross_links_when_disabled(
    mock_resolve,
    mock_extract,
    mock_sc_factory,
    mock_embed,
    mock_upsert_entity,
    mock_upsert_relation,
    mock_log,
    mock_discover,
):
    """Cross-link discovery should NOT be called when auto_entity_linking_enabled=False."""
    mock_resolve.return_value = _fake_config(auto_entity_linking_enabled=False)

    entity = MagicMock()
    entity.canonical_name = "Alice"
    entity.entity_type = "person"
    entity.role = "subject"
    graph = MagicMock()
    graph.entities = [entity]
    graph.relations = []
    mock_extract.return_value = graph

    sc = MagicMock()
    sc.find_entity_link = AsyncMock(return_value=None)
    sc.create_entity_link = AsyncMock()
    mock_sc_factory.return_value = sc

    mock_embed.return_value = [0.1] * 10

    upsert_result = MagicMock()
    upsert_result.id = uuid.uuid4()
    mock_upsert_entity.return_value = upsert_result

    memory_id = uuid.uuid4()

    with patch("core_api.tasks.track_task"):
        await process_entity_extraction(
            memory_id=memory_id,
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    mock_discover.assert_not_awaited()


@pytest.mark.asyncio
@patch(
    "core_api.services.entity_extraction_worker._discover_cross_links_for_memory",
    new_callable=AsyncMock,
)
@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.upsert_entity", new_callable=AsyncMock
)
@patch(
    "core_api.services.entity_extraction_worker.get_embedding", new_callable=AsyncMock
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_extraction_cross_link_failure_is_nonfatal(
    mock_resolve,
    mock_extract,
    mock_sc_factory,
    mock_embed,
    mock_upsert_entity,
    mock_upsert_relation,
    mock_log,
    mock_discover,
):
    """If cross-link discovery raises, the overall extraction should still succeed."""
    mock_resolve.return_value = _fake_config(auto_entity_linking_enabled=True)

    entity = MagicMock()
    entity.canonical_name = "Alice"
    entity.entity_type = "person"
    entity.role = "subject"
    graph = MagicMock()
    graph.entities = [entity]
    graph.relations = []
    mock_extract.return_value = graph

    sc = MagicMock()
    sc.find_entity_link = AsyncMock(return_value=None)
    sc.create_entity_link = AsyncMock()
    mock_sc_factory.return_value = sc

    mock_embed.return_value = [0.1] * 10

    upsert_result = MagicMock()
    upsert_result.id = uuid.uuid4()
    mock_upsert_entity.return_value = upsert_result

    mock_discover.side_effect = RuntimeError("boom")

    memory_id = uuid.uuid4()

    with patch("core_api.tasks.track_task"):
        # Should NOT raise — cross-link failure is non-fatal
        await process_entity_extraction(
            memory_id=memory_id,
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    mock_discover.assert_awaited_once()
