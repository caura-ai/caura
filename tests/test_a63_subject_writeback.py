"""A63 — the extraction worker writes the subject back to the memory row.

``memories.subject_entity_id`` was NULL on nearly every row (CAURA-123's
``EmitMemoryTriple`` only fires on narrow write-time predicate regexes),
which kept the deterministic RDF contradiction path and the A1 #17
cross-subject preflight dormant. The extraction LLM already names a
subject per entity; after link creation the worker now writes it back —
under strict conservatism:

- EXACTLY ONE distinct subject-role entity → write it back (storage-side
  guarded by ``subject_entity_id IS NULL``, triple-path value wins).
- Zero or 2+ distinct subjects → skip. A wrong subject is worse than
  none, because A1 #17 compares the column across rows as authoritative.
- Two surface forms resolving to the SAME entity id count as one subject.
- A write-back failure is non-fatal — extraction (and the Path C
  dispatch behind it) proceeds exactly as before.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core_api.services.entity_extraction_worker import process_entity_extraction

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _config(**overrides):
    cfg = MagicMock()
    cfg.auto_entity_linking_enabled = False
    cfg.entity_blocklist = frozenset()
    cfg.entity_extraction_provider = "openai"
    cfg.entity_extraction_model = "gpt-4o-mini"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _entity(name: str, entity_type: str = "person", role: str = "subject") -> MagicMock:
    e = MagicMock()
    e.canonical_name = name
    e.entity_type = entity_type
    e.role = role
    return e


def _graph(entities: list[MagicMock]) -> MagicMock:
    g = MagicMock()
    g.entities = entities
    g.relations = []
    return g


def _sc(entity_ids: list[str]) -> MagicMock:
    """Storage mock where entity i resolves as a fresh create with
    ``entity_ids[i]``."""
    sc = MagicMock()
    sc.bulk_resolve_entities = AsyncMock(return_value=[None] * len(entity_ids))
    sc.bulk_upsert_entities = AsyncMock(
        return_value=[
            {"input_idx": i, "entity_id": eid, "action": "created"}
            for i, eid in enumerate(entity_ids)
        ]
    )
    sc.bulk_upsert_entity_links = AsyncMock(
        return_value=[{"input_idx": i, "created": True} for i in range(len(entity_ids))]
    )
    sc.set_subject_entity_if_null = AsyncMock(return_value=True)
    return sc


async def _run(mock_extract_graph, sc, memory_id=None):
    with (
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=_config()),
        ),
        patch(
            "core_api.services.entity_extraction_worker.extract_entities_from_content",
            new=AsyncMock(return_value=mock_extract_graph),
        ),
        patch(
            "core_api.services.entity_extraction_worker.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.entity_extraction_worker.get_embedding",
            new=AsyncMock(return_value=[0.1] * 8),
        ),
        patch("core_api.services.entity_extraction_worker.log_action", new=AsyncMock()),
        patch("core_api.tasks.track_task"),
    ):
        await process_entity_extraction(
            memory_id=memory_id or uuid4(),
            tenant_id="t1",
            fleet_id=None,
            agent_id="a1",
            content="test content",
            memory_type="fact",
        )


async def test_single_subject_written_back() -> None:
    subject_id = str(uuid4())
    other_id = str(uuid4())
    sc = _sc([subject_id, other_id])
    memory_id = uuid4()

    await _run(
        _graph(
            [
                _entity("Priya Nair", role="subject"),
                _entity("ingest pipeline", "project", role="mentioned"),
            ]
        ),
        sc,
        memory_id=memory_id,
    )

    sc.set_subject_entity_if_null.assert_awaited_once_with(
        memory_id=str(memory_id), tenant_id="t1", subject_entity_id=subject_id
    )


async def test_multiple_distinct_subjects_skip() -> None:
    sc = _sc([str(uuid4()), str(uuid4())])
    await _run(
        _graph([_entity("Priya", role="subject"), _entity("Tom", role="subject")]), sc
    )
    sc.set_subject_entity_if_null.assert_not_awaited()


async def test_no_subject_role_skips() -> None:
    sc = _sc([str(uuid4()), str(uuid4())])
    await _run(
        _graph(
            [
                _entity("Acme", "organization", role="mentioned"),
                _entity("Bazel", "tool", role="object"),
            ]
        ),
        sc,
    )
    sc.set_subject_entity_if_null.assert_not_awaited()


async def test_two_surface_forms_same_entity_count_as_one_subject() -> None:
    shared_id = str(uuid4())
    sc = MagicMock()
    sc.bulk_resolve_entities = AsyncMock(return_value=[None, None])
    # Both surface forms upsert to the SAME entity row.
    sc.bulk_upsert_entities = AsyncMock(
        return_value=[
            {"input_idx": 0, "entity_id": shared_id, "action": "created"},
            {"input_idx": 1, "entity_id": shared_id, "action": "merged"},
        ]
    )
    sc.bulk_upsert_entity_links = AsyncMock(
        return_value=[{"input_idx": 0, "created": True}]
    )
    sc.set_subject_entity_if_null = AsyncMock(return_value=True)

    await _run(
        _graph(
            [_entity("Priya", role="subject"), _entity("Priya Nair", role="subject")]
        ),
        sc,
    )

    sc.set_subject_entity_if_null.assert_awaited_once()
    assert (
        sc.set_subject_entity_if_null.await_args.kwargs["subject_entity_id"]
        == shared_id
    )


async def test_already_set_row_is_a_benign_skip() -> None:
    sc = _sc([str(uuid4())])
    sc.set_subject_entity_if_null = AsyncMock(
        return_value=False
    )  # storage kept existing
    await _run(_graph([_entity("Priya", role="subject")]), sc)
    sc.set_subject_entity_if_null.assert_awaited_once()


async def test_writeback_failure_is_non_fatal() -> None:
    sc = _sc([str(uuid4())])
    sc.set_subject_entity_if_null = AsyncMock(side_effect=RuntimeError("storage down"))
    # Must not raise — extraction completes and downstream dispatch still runs.
    await _run(_graph([_entity("Priya", role="subject")]), sc)
    sc.set_subject_entity_if_null.assert_awaited_once()
