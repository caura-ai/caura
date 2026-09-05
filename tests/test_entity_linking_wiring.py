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
    # H-02: the worker re-reads the memory before persisting, so nothing is
    # written to the graph of a row governance dropped mid-extraction. These
    # tests exercise a live row.
    sc.get_memory = AsyncMock(return_value={"id": "m", "deleted_at": None})
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
    # H-02: the worker re-reads the memory before persisting, so nothing is
    # written to the graph of a row governance dropped mid-extraction. These
    # tests exercise a live row.
    sc.get_memory = AsyncMock(return_value={"id": "m", "deleted_at": None})
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
    # H-02: the worker re-reads the memory before persisting, so nothing is
    # written to the graph of a row governance dropped mid-extraction. These
    # tests exercise a live row.
    sc.get_memory = AsyncMock(return_value={"id": "m", "deleted_at": None})
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


# ── H-02: a row governance dropped mid-extraction gets no graph rows ──


def _one_entity_graph():
    entity = MagicMock()
    entity.canonical_name = "Alice"
    entity.entity_type = "person"
    entity.role = "subject"
    graph = MagicMock()
    graph.entities = [entity]
    graph.relations = []
    return graph


def _graph_sc(*, deleted_at):
    sc = MagicMock()
    sc.get_memory = AsyncMock(return_value={"id": "m", "deleted_at": deleted_at})
    sc.bulk_resolve_entities = AsyncMock(return_value=[None])
    # ``entity_id`` + ``input_idx`` is the shape the worker actually reads; a bare
    # ``id`` leaves name_to_id empty, so no links are built and the write path
    # quietly ends early.
    sc.bulk_upsert_entities = AsyncMock(
        return_value=[
            {"entity_id": str(uuid.uuid4()), "input_idx": 0, "action": "created"}
        ]
    )
    sc.bulk_upsert_entity_links = AsyncMock(
        return_value=[{"input_idx": 0, "created": True}]
    )
    sc.find_entity_link = AsyncMock(return_value=None)
    sc.create_entity_link = AsyncMock()
    sc.purge_entity_artifacts = AsyncMock(
        return_value={"links": 1, "relations": 0, "entities": 1}
    )
    # Reached only by the tests that run the write path to completion — the
    # dropped-before-extraction case returns before it.
    sc.discover_cross_links = AsyncMock(return_value={"links_created": 0})
    sc.update_memory = AsyncMock()
    return sc


@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.get_embedding", new_callable=AsyncMock
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_a_memory_dropped_during_extraction_gets_no_entities(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, mock_log
):
    """H-02. Extraction is scheduled at write time, in parallel with the
    enrichment that carries the governance verdict — so the row can already be
    gone by the time the LLM call returns.

    Writing entities for it would put the dropped content's names into a table
    the drop does not reach, listable tenant-wide through ``/entities`` and
    ``/graph``.

    This is the cheap case: the row was already gone when the LLM call returned,
    so no work is done at all. It does NOT close the window on its own — a drop
    landing during the writes is covered by the post-write purge, pinned below.
    """
    mock_resolve.return_value = _fake_config()
    mock_extract.return_value = _one_entity_graph()
    mock_embed.return_value = None
    sc = _graph_sc(deleted_at="2026-09-05T00:00:00Z")
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task"):
        await process_entity_extraction(
            memory_id=uuid.uuid4(),
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    # Asserted on the WRITES, not on the early return, so a refactor that keeps
    # the check but persists anyway still fails.
    sc.bulk_upsert_entities.assert_not_awaited()
    sc.bulk_upsert_entity_links.assert_not_awaited()


@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.get_embedding", new_callable=AsyncMock
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_the_liveness_check_reads_the_writer(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, mock_log
):
    """The check exists to observe a delete that just committed.

    A replica under lag would report the row live, so the check would pass
    exactly when it most needed to fail.
    """
    mock_resolve.return_value = _fake_config()
    mock_extract.return_value = _one_entity_graph()
    mock_embed.return_value = None
    sc = _graph_sc(deleted_at=None)
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task"):
        await process_entity_extraction(
            memory_id=uuid.uuid4(),
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    assert sc.get_memory.await_args.kwargs.get("read") is False, (
        sc.get_memory.await_args
    )


@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.get_embedding", new_callable=AsyncMock
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_a_drop_landing_during_the_writes_purges_what_was_just_written(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, mock_log
):
    """The window the pre-write check cannot close.

    The row is live when extraction finishes, so the early check passes and the
    writes proceed. Governance drops it during those writes — its own purge runs
    while these rows do not exist yet and finds nothing. Without the post-write
    re-check the entities land afterwards and nothing ever revisits them: the
    memory is gone, so no later verdict names it.
    """
    mock_resolve.return_value = _fake_config()
    mock_extract.return_value = _one_entity_graph()
    mock_embed.return_value = None
    sc = _graph_sc(deleted_at=None)
    # Live at the pre-write check, dropped by the post-write one.
    sc.get_memory = AsyncMock(
        side_effect=[
            {"id": "m", "deleted_at": None},
            {"id": "m", "deleted_at": "2026-09-05T00:00:00Z"},
        ]
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task"):
        await process_entity_extraction(
            memory_id=uuid.uuid4(),
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    # The writes DID happen — that is the premise, not a failure.
    sc.bulk_upsert_entity_links.assert_awaited()
    # And they were taken back.
    sc.purge_entity_artifacts.assert_awaited_once()


@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch(
    "core_api.services.entity_extraction_worker.get_embedding", new_callable=AsyncMock
)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_a_row_still_live_after_the_writes_is_not_purged(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, mock_log
):
    """OVER-REFUSAL GUARD, and the one that matters most here.

    The ordinary path writes entities for a live memory. A post-write purge that
    fired on it would delete the graph rows of every successfully extracted
    memory in the install — the failure mode of this fix is far worse than the
    leak it closes, so it gets its own test rather than riding on the one above.
    """
    mock_resolve.return_value = _fake_config()
    mock_extract.return_value = _one_entity_graph()
    mock_embed.return_value = None
    sc = _graph_sc(deleted_at=None)
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task"):
        await process_entity_extraction(
            memory_id=uuid.uuid4(),
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    sc.bulk_upsert_entity_links.assert_awaited()
    sc.purge_entity_artifacts.assert_not_awaited()
