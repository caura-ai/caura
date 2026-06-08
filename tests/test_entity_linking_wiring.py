"""Integration wiring tests for entity-linking on the synchronous
write path (CAURA-657 removed the lifecycle-side wiring; the daily
fanout for crystallize + entity-link now lives on its own Pub/Sub
topics tested in test_lifecycle_handlers.py).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.services.entity_extraction_worker import (
    process_entity_extraction,
)


# ── Helpers ───────────────────────────────────────────────────────────


def _fake_config(**overrides):
    """Return a mock ResolvedConfig with sensible defaults."""
    cfg = MagicMock()
    cfg.auto_entity_linking_enabled = True
    cfg.entity_blocklist = frozenset()
    cfg.entity_extraction_provider = "openai"
    cfg.entity_extraction_model = "gpt-4o-mini"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


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

    # Plumb the post-P1 bulk flow: resolve returns ``None`` (no
    # existing match), so the worker takes the create path. The
    # resulting entity_id is what populates ``name_to_id`` and
    # gates the downstream cross-link discovery trigger.
    sc.bulk_resolve_entities = AsyncMock(return_value=[None])
    sc.bulk_upsert_entities = AsyncMock(
        return_value=[
            {"input_idx": 0, "entity_id": str(uuid.uuid4()), "action": "created"}
        ]
    )
    sc.bulk_upsert_entity_links = AsyncMock(
        return_value=[{"input_idx": 0, "created": True}]
    )

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

    # Plumb the post-P1 bulk flow: resolve returns ``None`` (no
    # existing match), so the worker takes the create path. The
    # resulting entity_id is what populates ``name_to_id`` and
    # gates the downstream cross-link discovery trigger.
    sc.bulk_resolve_entities = AsyncMock(return_value=[None])
    sc.bulk_upsert_entities = AsyncMock(
        return_value=[
            {"input_idx": 0, "entity_id": str(uuid.uuid4()), "action": "created"}
        ]
    )
    sc.bulk_upsert_entity_links = AsyncMock(
        return_value=[{"input_idx": 0, "created": True}]
    )

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

    # Plumb the post-P1 bulk flow: resolve returns ``None`` (no
    # existing match), so the worker takes the create path. The
    # resulting entity_id is what populates ``name_to_id`` and
    # gates the downstream cross-link discovery trigger.
    sc.bulk_resolve_entities = AsyncMock(return_value=[None])
    sc.bulk_upsert_entities = AsyncMock(
        return_value=[
            {"input_idx": 0, "entity_id": str(uuid.uuid4()), "action": "created"}
        ]
    )
    sc.bulk_upsert_entity_links = AsyncMock(
        return_value=[{"input_idx": 0, "created": True}]
    )

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
