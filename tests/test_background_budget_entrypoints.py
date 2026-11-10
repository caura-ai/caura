"""Background entry points carry tenant admission into the shared retry layer."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from common.enrichment.service import fake_enrich
from common.events.base import Event
from common.llm import retry
from core_api.services import contradiction_detector as detector
from core_api.services import entity_extraction_worker as extraction
from core_api.services.entity_extraction import ExtractedGraph
from core_worker import consumer

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def boundaries(monkeypatch):
    reserve = AsyncMock(return_value={"allowed": False, "retry_after_seconds": 60})
    monkeypatch.setattr(retry, "reserve_background_attempt", reserve, raising=False)
    provider = AsyncMock(return_value="model answer")
    sc = MagicMock()
    sc.defer_background_work = AsyncMock(return_value={"queued": True})
    sc.get_memory = AsyncMock(
        return_value={
            "id": str(uuid4()),
            "tenant_id": "tenant-a",
            "content": "synthetic fact",
            "agent_id": "writer",
            "status": "active",
            "visibility": "scope_team",
            "deleted_at": None,
            "supersedes_id": None,
        }
    )
    sc.add_task_failure = AsyncMock()
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=None),
    )
    return SimpleNamespace(reserve=reserve, provider=provider, storage=sc)


async def _provider_call(boundaries, operation):
    return await retry.call_with_retry(boundaries.provider, operation, max_attempts=1)


async def test_entity_extraction_stops_before_a_provider_when_the_tenant_is_exhausted(
    boundaries, monkeypatch
):
    async def extract(*_a, **_kw):
        await _provider_call(boundaries, "entity-extraction")
        return ExtractedGraph(entities=[], relations=[])

    monkeypatch.setattr(extraction, "extract_entities_from_content", extract)
    monkeypatch.setattr(extraction, "get_storage_client", lambda: boundaries.storage)
    monkeypatch.setattr(extraction, "record_task_failure", AsyncMock())
    await extraction.process_entity_extraction(
        uuid4(), "tenant-a", None, "writer", "synthetic fact", "fact"
    )

    assert boundaries.provider.await_count == 0, (
        "background extraction reached a provider"
    )
    assert boundaries.reserve.await_count == 1
    assert boundaries.storage.defer_background_work.await_count == 1


@pytest.mark.parametrize("path", ["content", "entity"])
async def test_both_detection_entry_points_enforce_the_tenant_budget(
    boundaries, monkeypatch, path
):
    monkeypatch.setattr(detector, "_DETECTION_GATE", None)
    monkeypatch.setattr(detector, "get_storage_client", lambda: boundaries.storage)
    monkeypatch.setattr(
        detector, "_detection_disabled_for_tenant", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(detector, "cache_set_nx", AsyncMock(return_value=True))
    monkeypatch.setattr(detector, "cache_delete_if", AsyncMock(return_value=True))
    monkeypatch.setattr(
        detector, "_retract_unlinked_verdicts", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(
        detector, "_attempt_entity_retraction", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        detector,
        "_rdf_conflict_pass",
        AsyncMock(
            return_value=SimpleNamespace(
                contradictions=[],
                disputed_pairs=[],
            )
        ),
    )

    async def judge(*_a, **_kw):
        await _provider_call(boundaries, "contradiction")
        return []

    monkeypatch.setattr(detector, "_detect", judge)
    boundaries.storage.find_entity_overlap_candidates = judge
    mid = uuid4()
    if path == "content":
        await detector.detect_contradictions_async(
            mid, "tenant-a", None, "synthetic fact", [0.1] * 8
        )
    else:
        await detector.detect_contradictions_by_entities_async(mid, "tenant-a", None)

    assert boundaries.provider.await_count == 0, f"{path} detection reached a provider"
    assert boundaries.reserve.await_count == 1
    assert boundaries.storage.defer_background_work.await_count == 1


async def test_worker_enrichment_defers_without_patching_or_publishing_a_heuristic(
    boundaries, monkeypatch
):
    async def enrich(content, *_a, **_kw):
        await _provider_call(boundaries, "enrichment")
        return fake_enrich(content)

    patch = AsyncMock(return_value=True)
    publish = AsyncMock()
    monkeypatch.setattr(consumer, "_storage_client_factory", lambda: boundaries.storage)
    monkeypatch.setattr(consumer, "enrich_memory", enrich)
    monkeypatch.setattr(consumer, "update_memory_enrichment", patch)
    monkeypatch.setattr(consumer, "publish_memory_enriched", publish)
    event = Event(
        event_type="caura.memory.enrich-requested",
        payload={
            "memory_id": str(uuid4()),
            "tenant_id": "tenant-a",
            "content": "synthetic fact",
        },
    )
    await consumer.handle_enrich_request(event)

    assert boundaries.provider.await_count == 0, (
        "deferred worker enrichment reached a provider"
    )
    assert boundaries.reserve.await_count == 1
    assert boundaries.storage.defer_background_work.await_count == 1
    patch.assert_not_awaited()
    publish.assert_not_awaited()


@pytest.mark.parametrize("failure", ["exception", "timeout", "unacknowledged"])
async def test_worker_keeps_the_event_retryable_when_deferral_is_not_durable(
    boundaries, monkeypatch, caplog, failure
):
    async def enrich(*_a, **_kw):
        from common.background_work import BackgroundWorkDeferred

        raise BackgroundWorkDeferred("budget_exhausted")

    if failure == "unacknowledged":
        boundaries.storage.defer_background_work.return_value = {"queued": False}
    else:
        boundaries.storage.defer_background_work.side_effect = (
            TimeoutError()
            if failure == "timeout"
            else RuntimeError("storage body to withhold")
        )
    patch, publish = AsyncMock(), AsyncMock()
    monkeypatch.setattr(consumer, "_storage_client_factory", lambda: boundaries.storage)
    monkeypatch.setattr(consumer, "enrich_memory", enrich)
    monkeypatch.setattr(consumer, "update_memory_enrichment", patch)
    monkeypatch.setattr(consumer, "publish_memory_enriched", publish)
    event = Event(
        event_type="caura.memory.enrich-requested",
        payload={
            "memory_id": str(uuid4()),
            "tenant_id": "tenant-a",
            "content": "synthetic fact",
        },
    )
    with pytest.raises(RuntimeError, match="background retry unavailable"):
        await consumer.handle_enrich_request(event)
    patch.assert_not_awaited()
    publish.assert_not_awaited()
    assert "retry_unavailable" in [getattr(r, "reason", None) for r in caplog.records]
    assert "storage body to withhold" not in caplog.text
