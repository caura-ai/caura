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
from tests.conftest import close_scheduled_coro

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

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
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

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
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

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
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


def _graph_with_relation():
    """Like ``_one_entity_graph`` but the LLM also proposed a relation.

    ``graph.relations = []`` everywhere else in this file is what hid the
    post-purge fall-through: the relation upsert loop had nothing to iterate, so
    no test noticed the function carrying on writing after a detected drop.
    """
    graph = _one_entity_graph()
    rel = MagicMock()
    rel.from_entity = "Alice"
    rel.to_entity = "Alice"
    rel.relation_type = "knows"
    graph.relations = [rel]
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

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
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

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
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

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
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

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
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


@patch(
    "core_api.services.entity_extraction_worker._discover_cross_links_for_memory",
    new_callable=AsyncMock,
)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
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
async def test_a_detected_drop_stops_the_writes_that_come_after_the_purge(
    mock_resolve,
    mock_extract,
    mock_sc_factory,
    mock_embed,
    mock_log,
    mock_upsert_relation,
    mock_discover,
):
    """Purging is only half of it — the function must also STOP.

    Everything after the link upsert writes more graph rows for this memory: a
    relation carrying ``evidence_memory_id``, the subject write-back, cross-link
    discovery. Purging and then falling through cleans the link table and
    immediately refills the relation table, which moves the leak rather than
    closing it.

    Every other test in this file leaves ``graph.relations`` empty, so the
    relation loop had nothing to iterate and none of them could have caught it.
    """
    mock_resolve.return_value = _fake_config()
    mock_extract.return_value = _graph_with_relation()
    mock_embed.return_value = None
    sc = _graph_sc(deleted_at=None)
    sc.get_memory = AsyncMock(
        side_effect=[
            {"id": "m", "deleted_at": None},
            {"id": "m", "deleted_at": "2026-09-05T00:00:00Z"},
        ]
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid.uuid4(),
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    sc.purge_entity_artifacts.assert_awaited_once()
    # The rows the purge would NOT have covered, because they did not exist yet.
    mock_upsert_relation.assert_not_awaited()
    mock_discover.assert_not_awaited()


@patch(
    "core_api.services.entity_extraction_worker._discover_cross_links_for_memory",
    new_callable=AsyncMock,
)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
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
async def test_a_live_row_still_gets_its_relations_written(
    mock_resolve,
    mock_extract,
    mock_sc_factory,
    mock_embed,
    mock_log,
    mock_upsert_relation,
    mock_discover,
):
    """The other half of the guard above: stopping must be conditional.

    A short-circuit that fired on live rows would silently stop writing relations
    and cross-links for every extracted memory in the install.
    """
    mock_resolve.return_value = _fake_config()
    mock_extract.return_value = _graph_with_relation()
    mock_embed.return_value = None
    sc = _graph_sc(deleted_at=None)
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid.uuid4(),
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    sc.purge_entity_artifacts.assert_not_awaited()
    mock_upsert_relation.assert_awaited()
    mock_discover.assert_awaited()


@patch(
    "core_api.services.entity_extraction_worker._discover_cross_links_for_memory",
    new_callable=AsyncMock,
)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
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
async def test_a_drop_landing_after_the_relation_write_is_still_purged(
    mock_resolve,
    mock_extract,
    mock_sc_factory,
    mock_embed,
    mock_log,
    mock_upsert_relation,
    mock_discover,
):
    """The window the post-link check does NOT cover.

    The row is live at the link check, so the function proceeds — and writes the
    subject pointer, the relations carrying ``evidence_memory_id``, and whatever
    cross-link discovery creates. A drop landing across THAT stretch runs its own
    purge against rows that do not exist yet, and nothing revisits them.

    Only a check after every graph-mutating write catches it. A single check
    after the link upsert reports success on this case while leaving the relation
    row live for dropped content.
    """
    mock_resolve.return_value = _fake_config()
    mock_extract.return_value = _graph_with_relation()
    mock_embed.return_value = None
    sc = _graph_sc(deleted_at=None)
    # Live at the pre-write check AND at the post-link check; dropped only by the
    # final one, after the relation and cross-link writes.
    sc.get_memory = AsyncMock(
        side_effect=[
            {"id": "m", "deleted_at": None},
            {"id": "m", "deleted_at": None},
            {"id": "m", "deleted_at": "2026-09-05T00:00:00Z"},
        ]
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid.uuid4(),
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    # The relation WAS written — that is the premise of this race, not a failure.
    mock_upsert_relation.assert_awaited()
    # And the rows it left behind were taken back.
    sc.purge_entity_artifacts.assert_awaited_once()


@patch(
    "core_api.services.entity_extraction_worker._discover_cross_links_for_memory",
    new_callable=AsyncMock,
)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
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
async def test_a_transient_liveness_read_failure_does_not_discard_the_audit_work(
    mock_resolve,
    mock_extract,
    mock_sc_factory,
    mock_embed,
    mock_log,
    mock_upsert_relation,
    mock_discover,
):
    """A read timeout says nothing about whether the memory was dropped.

    An earlier revision returned a bool meaning "you must stop", which forced the
    indeterminate case to pick one of the two real answers. It picked "stop", and
    a transient storage blip then silently discarded the audit-log entry, the
    contradiction trigger and cross-link discovery for a memory that was almost
    certainly still live — with nothing to repair it, since the task is
    fire-and-forget.

    ``unknown`` is now its own answer and this call site acts only on ``dropped``.
    The final check is the leak guarantee; this one is only an optimisation, so
    it has no business destroying work on a non-answer.
    """
    mock_resolve.return_value = _fake_config()
    mock_extract.return_value = _graph_with_relation()
    mock_embed.return_value = None
    sc = _graph_sc(deleted_at=None)
    sc.get_memory = AsyncMock(
        side_effect=[
            {"id": "m", "deleted_at": None},  # pre-write check: live
            RuntimeError("storage read timed out"),  # post-link check: indeterminate
            {"id": "m", "deleted_at": None},  # final check: live after all
        ]
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid.uuid4(),
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    # Nothing was concluded, so nothing was destroyed.
    sc.purge_entity_artifacts.assert_not_awaited()
    assert mock_log.await_count == 1, "the audit entry was discarded on a non-answer"
    mock_upsert_relation.assert_awaited()
    mock_discover.assert_awaited()


@patch(
    "core_api.services.entity_extraction_worker._discover_cross_links_for_memory",
    new_callable=AsyncMock,
)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
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
async def test_a_crash_after_the_writes_still_purges_a_dropped_row(
    mock_resolve,
    mock_extract,
    mock_sc_factory,
    mock_embed,
    mock_log,
    mock_upsert_relation,
    mock_discover,
):
    """The exit the final check does not cover: leaving by raising.

    The check at the end of the ``try`` is the leak guarantee for the path that
    finishes. It is unreachable on the path that does not — anything between the
    link upsert and it that raises (a relation upsert, the subject write-back,
    the audit call) jumps straight to ``except``, and the entities and links
    already committed stay behind for a memory that may have been dropped. That
    is the H-02 leak itself, arriving through a door the fix had left open.

    The post-link check does not save this case either: it is allowed to fall
    through on ``unknown``, and here it is answered ``live`` — the drop lands
    afterwards, which is the whole reason a later check exists.
    """
    mock_resolve.return_value = _fake_config()
    mock_extract.return_value = _graph_with_relation()
    mock_embed.return_value = None
    sc = _graph_sc(deleted_at=None)
    sc.get_memory = AsyncMock(
        side_effect=[
            {"id": "m", "deleted_at": None},  # pre-write check: live
            {"id": "m", "deleted_at": None},  # post-link check: still live
            {"id": "m", "deleted_at": "2026-09-06T00:00:00Z"},  # except path: dropped
        ]
    )
    mock_sc_factory.return_value = sc
    # Raised after the links are committed and before the audit call, so the
    # function leaves with graph rows written and no normal-path check run.
    mock_upsert_relation.side_effect = RuntimeError("storage went away mid-write")

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        # Extraction is fire-and-forget; swallowing is the established contract
        # and not what this test is about.
        await process_entity_extraction(
            memory_id=uuid.uuid4(),
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    # It really did leave by raising — otherwise this passes for the wrong reason.
    mock_log.assert_not_awaited()
    assert sc.get_memory.await_count == 3, (
        "the except path never asked whether the memory survived"
    )
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
async def test_a_crash_before_any_write_costs_no_extra_liveness_read(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, mock_log
):
    """Guard on the FIX, not a reproducer for the bug — it passes either way.

    The obvious form of the fix is an unconditional check in ``except``. That
    spends a writer read on every failed extraction, including the failures that
    wrote nothing at all, on a path that runs for essentially every memory. The
    flag is what keeps the cost proportional to what is actually at risk.

    Here the resolve step fails, so no entity or link row was ever written and
    there is nothing to purge. The only liveness read should be the pre-write
    check that already ran.
    """
    mock_resolve.return_value = _fake_config()
    mock_extract.return_value = _one_entity_graph()
    mock_embed.return_value = None
    sc = _graph_sc(deleted_at=None)
    sc.bulk_resolve_entities = AsyncMock(side_effect=RuntimeError("resolve failed"))
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid.uuid4(),
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    sc.bulk_upsert_entities.assert_not_awaited()
    assert sc.get_memory.await_count == 1, (
        "a failure that wrote nothing still paid for a writer read"
    )
    sc.purge_entity_artifacts.assert_not_awaited()


@patch("core_api.services.entity_extraction_worker.log_action", new_callable=AsyncMock)
@patch("core_api.services.entity_extraction_worker.get_storage_client")
@patch(
    "core_api.services.entity_extraction_worker.extract_entities_from_content",
    new_callable=AsyncMock,
)
@patch("core_api.services.organization_settings.resolve_config", new_callable=AsyncMock)
async def test_a_crash_before_the_storage_client_exists_does_not_raise(
    mock_resolve, mock_extract, mock_sc_factory, mock_log
):
    """The other reason the ``except`` check is guarded: ``sc`` may not exist.

    ``get_storage_client`` is called after extraction, so a failure in the LLM
    call leaves the name unbound. An unguarded check in the handler raises
    ``NameError`` out of a fire-and-forget task, turning a logged non-fatal
    failure into an unhandled task exception. Also passes without the fix.
    """
    mock_resolve.return_value = _fake_config()
    mock_extract.side_effect = RuntimeError("extraction provider is down")

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid.uuid4(),
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    mock_sc_factory.assert_not_called()


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
async def test_an_entity_upsert_that_raises_is_still_treated_as_maybe_written(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, mock_log
):
    """Pins WHERE the flag is set: before the upsert await, not after.

    "The call raised" is not "nothing was written". A timeout can land on a
    request storage already committed, and then the rows exist while the caller
    only ever saw an exception. Setting the flag after the await would skip the
    liveness check on exactly that case and leak the rows — the H-02 bug again,
    through the narrowest door yet.

    The cost of being wrong the other way is one writer read on a call that
    never landed, on a path that is already failing. That asymmetry is the whole
    argument for the ordering, and moving the assignment one line down is an
    easy, invisible way to undo it.
    """
    mock_resolve.return_value = _fake_config()
    mock_extract.return_value = _one_entity_graph()
    mock_embed.return_value = None
    sc = _graph_sc(deleted_at=None)
    sc.bulk_upsert_entities = AsyncMock(
        side_effect=RuntimeError("timed out waiting for the upsert response")
    )
    sc.get_memory = AsyncMock(
        side_effect=[
            {"id": "m", "deleted_at": None},  # pre-write check: live
            {"id": "m", "deleted_at": "2026-09-06T00:00:00Z"},  # except path: dropped
        ]
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid.uuid4(),
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    assert sc.get_memory.await_count == 2, (
        "a write that may have committed was treated as certainly not written"
    )
    sc.purge_entity_artifacts.assert_awaited_once()


@patch(
    "core_api.services.entity_extraction_worker._discover_cross_links_for_memory",
    new_callable=AsyncMock,
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
async def test_cross_link_discovery_alone_still_owes_the_memory_a_liveness_check(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, mock_log, mock_discover
):
    """The persistence block is not the only thing that writes graph rows.

    Cross-link discovery inserts ``memory_entity_links`` rows for this memory —
    storage-side it is an ON CONFLICT DO NOTHING insert into exactly the table
    the purge deletes from — and it is gated on ``auto_entity_linking_enabled``
    alone. It runs when every extracted name was filtered out and
    ``bulk_upsert_entities`` was therefore never called.

    So ``wrote_graph_rows`` has to mean "this memory may have graph rows", not
    "the persistence block ran". Gating the final check on the narrower reading
    skips the purge on this path and leaks the links discovery just created for
    a dropped memory — H-02 again, through the fix's own guard.
    """
    # Every extracted name is blocklisted, so ``filtered`` ends up empty and the
    # whole persistence branch is skipped.
    mock_resolve.return_value = _fake_config(entity_blocklist=frozenset({"alice"}))
    mock_extract.return_value = _one_entity_graph()
    mock_embed.return_value = None
    sc = _graph_sc(deleted_at=None)
    sc.get_memory = AsyncMock(
        side_effect=[
            {"id": "m", "deleted_at": None},  # pre-write check: live
            {"id": "m", "deleted_at": "2026-09-06T00:00:00Z"},  # final check: dropped
        ]
    )
    mock_sc_factory.return_value = sc

    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid.uuid4(),
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    # The premise: nothing went through the persistence block, and discovery ran.
    sc.bulk_upsert_entities.assert_not_awaited()
    mock_discover.assert_awaited_once()
    sc.purge_entity_artifacts.assert_awaited_once()


@patch(
    "core_api.services.entity_extraction_worker._discover_cross_links_for_memory",
    new_callable=AsyncMock,
)
@patch(
    "core_api.services.entity_extraction_worker.upsert_relation", new_callable=AsyncMock
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
async def test_an_unreadable_purge_response_does_not_escape_the_except_handler(
    mock_resolve,
    mock_extract,
    mock_sc_factory,
    mock_embed,
    mock_log,
    mock_upsert_relation,
    mock_discover,
):
    """The purge response shape is not guaranteed, and one call site is exposed.

    ``_post`` is typed ``dict | list`` with a ``type: ignore`` at the call, so a
    2xx carrying a list reaches ``counts.get`` — outside the try that wraps the
    purge — and raises ``AttributeError``.

    Two of this helper's three call sites sit inside ``process_entity_extraction``'s
    catch-all, which would absorb that. The third does not: it IS the catch-all,
    and an exception raised inside an ``except`` block propagates out of the
    function, surfacing as an unhandled exception on a fire-and-forget task.

    So this drives the except-handler site specifically — a relation upsert fails
    after the links are committed, the memory turns out to be dropped, and the
    purge answers with a list.
    """
    mock_resolve.return_value = _fake_config()
    mock_extract.return_value = _graph_with_relation()
    mock_embed.return_value = None
    sc = _graph_sc(deleted_at=None)
    sc.get_memory = AsyncMock(
        side_effect=[
            {"id": "m", "deleted_at": None},  # pre-write check: live
            {"id": "m", "deleted_at": None},  # post-link check: still live
            {"id": "m", "deleted_at": "2026-09-06T00:00:00Z"},  # except path: dropped
        ]
    )
    sc.purge_entity_artifacts = AsyncMock(return_value=["unexpected", "shape"])
    mock_sc_factory.return_value = sc
    mock_upsert_relation.side_effect = RuntimeError("storage went away mid-write")

    # The assertion IS that this returns rather than raising.
    with patch("core_api.tasks.track_task", side_effect=close_scheduled_coro):
        await process_entity_extraction(
            memory_id=uuid.uuid4(),
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    # And it really did take the except-handler path with the purge attempted.
    assert sc.get_memory.await_count == 3
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
async def test_entities_committed_without_their_links_are_named_in_the_log(
    mock_resolve, mock_extract, mock_sc_factory, mock_embed, mock_log, caplog
):
    """The one gap the purge cannot close from its own side.

    ``memory_purge_entity_artifacts`` finds entities THROUGH the memory's links,
    deliberately: the first draft swept every unlinked entity in the tenant, which
    over-deletes and races a concurrent writer that has created an entity but not
    yet linked it.

    The cost of that bounding lands exactly here. If the entity upsert commits and
    the link upsert then raises, those rows are reachable by nothing — so a purge
    for this memory runs, finds no links, and reports ``{0, 0, 0}``. That is
    indistinguishable in the audit trail from "there was nothing to purge", which
    is what makes it a silent miss rather than a loud one.

    It stays a miss; what changes is that a person is handed the ids. Widening the
    purge's candidate set instead would re-introduce the race the bounding exists
    to prevent.

    Only entities this run CREATED are named. One that already existed is
    reachable through whatever linked it before and is nobody's orphan.
    """
    mock_resolve.return_value = _fake_config()
    mock_extract.return_value = _one_entity_graph()
    mock_embed.return_value = None
    sc = _graph_sc(deleted_at=None)
    fresh = str(uuid.uuid4())
    sc.bulk_upsert_entities = AsyncMock(
        return_value=[{"entity_id": fresh, "input_idx": 0, "action": "created"}]
    )
    sc.bulk_upsert_entity_links = AsyncMock(
        side_effect=RuntimeError("link upsert never landed")
    )
    mock_sc_factory.return_value = sc

    with (
        caplog.at_level("ERROR"),
        patch("core_api.tasks.track_task", side_effect=close_scheduled_coro),
    ):
        await process_entity_extraction(
            memory_id=uuid.uuid4(),
            tenant_id="test-tenant",
            fleet_id=None,
            agent_id="test-agent",
            content="Alice loves coffee",
            memory_type="episodic",
        )

    assert any(fresh in r.getMessage() for r in caplog.records), (
        "an entity row that no link and no purge can reach went unnamed in the log"
    )
