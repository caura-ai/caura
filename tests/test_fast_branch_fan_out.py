"""ScheduleBackgroundTasks fast-branch fan-out (closes Gaps 01 + 04).

The fast branch returns BEFORE the strong branch's entity-extraction +
Path A blocks. Historically each had to be wired through
``_enrich_memory_background`` indirectly, which left coverage holes:

- **Gap 01**: Enterprise+fast wrote rows with zero entity_links because
  the deferred-enrich worker handler doesn't fire
  ``process_entity_extraction`` and nothing further in the chain does
  either.
- **Gap 04**: OSS+fast lost Path A because the gate inside
  ``_enrich_memory_background`` (``not settings.embed_on_hot_path``)
  evaluated False in the OSS profile.

These tests pin the new direct fan-out in the fast branch as a
regression guard.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core_api.constants import VECTOR_DIM
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.write.schedule_background_tasks import (
    ScheduleBackgroundTasks,
)
from core_api.schemas import MemoryCreate

pytestmark = pytest.mark.asyncio


TENANT_ID = f"test-fast-fanout-{uuid.uuid4().hex[:8]}"


def _input() -> MemoryCreate:
    return MemoryCreate(
        tenant_id=TENANT_ID,
        fleet_id="f",
        agent_id="a",
        content="A test memory long enough to pass any content-length gate.",
        persist=True,
        entity_links=[],
    )


def _tenant_config(
    *, entity_extraction: bool = True, enrichment: bool = True
) -> SimpleNamespace:
    return SimpleNamespace(
        enrichment_enabled=enrichment,
        enrichment_provider="fake" if enrichment else "none",
        entity_extraction_enabled=entity_extraction,
    )


def _ctx(
    *,
    embedding: list[float] | None,
    entity_extraction: bool = True,
    enrichment: bool = True,
    memory_id: uuid.UUID | None = None,
) -> PipelineContext:
    return PipelineContext(
                data={
            "input": _input(),
            "memory": {"id": memory_id or uuid.uuid4()},
            "embedding": embedding,
            "enrichment": None,
            "resolved_write_mode": "fast",
            "content_hash": "f" * 64,
        },
        tenant_config=_tenant_config(
            entity_extraction=entity_extraction, enrichment=enrichment
        ),
    )


# ---------------------------------------------------------------------------
# Gap 01 — entity extraction fires from the fast branch directly
# ---------------------------------------------------------------------------


async def test_fast_fires_entity_extraction_when_enabled() -> None:
    """Closes Gap 01: prior to this PR, Enterprise+fast wrote rows with
    zero entity_links because nothing in the fast chain fired extraction.
    Now the fast branch fires it directly, mirroring the strong branch."""
    ctx = _ctx(embedding=[0.1] * VECTOR_DIM)
    extract_spy = AsyncMock(return_value=None)
    with (
        patch(
            "core_api.pipeline.steps.write.schedule_background_tasks.process_entity_extraction",
            new=extract_spy,
        ),
        patch(
            "core_api.services.memory_service._schedule_enrich_or_inline",
            new=AsyncMock(),
        ),
        patch(
            "core_api.services.contradiction_detector.detect_contradictions_async",
            new=AsyncMock(),
        ),
    ):
        await ScheduleBackgroundTasks().execute(ctx)

    extract_spy.assert_called_once()


async def test_fast_skips_entity_extraction_when_disabled() -> None:
    """Tenant-level kill-switch must still work."""
    ctx = _ctx(embedding=[0.1] * VECTOR_DIM, entity_extraction=False)
    extract_spy = AsyncMock(return_value=None)
    with (
        patch(
            "core_api.pipeline.steps.write.schedule_background_tasks.process_entity_extraction",
            new=extract_spy,
        ),
        patch(
            "core_api.services.memory_service._schedule_enrich_or_inline",
            new=AsyncMock(),
        ),
        patch(
            "core_api.services.contradiction_detector.detect_contradictions_async",
            new=AsyncMock(),
        ),
    ):
        await ScheduleBackgroundTasks().execute(ctx)

    extract_spy.assert_not_called()


# ---------------------------------------------------------------------------
# Gap 04 — Path A fires from the fast branch when embedding is present
# ---------------------------------------------------------------------------


async def test_fast_fires_path_a_when_embedding_present() -> None:
    """Closes Gap 04: OSS+fast (embedding inline because
    ``embed_on_hot_path=True``) must fire Path A. The pre-PR-3 path
    through ``_enrich_memory_background`` was gated on
    ``not settings.embed_on_hot_path``, so OSS+fast never got Path A."""
    ctx = _ctx(embedding=[0.1] * VECTOR_DIM)
    contradiction_spy = AsyncMock(return_value=None)
    with (
        patch(
            "core_api.services.contradiction_detector.detect_contradictions_async",
            new=contradiction_spy,
        ),
        patch(
            "core_api.pipeline.steps.write.schedule_background_tasks.process_entity_extraction",
            new=AsyncMock(),
        ),
        patch(
            "core_api.services.memory_service._schedule_enrich_or_inline",
            new=AsyncMock(),
        ),
    ):
        await ScheduleBackgroundTasks().execute(ctx)

    contradiction_spy.assert_called_once()


async def test_fast_skips_path_a_when_embedding_none() -> None:
    """When embedding is None (Enterprise+fast deferred), Path A is NOT
    fired from the fast branch. The ``EMBEDDED`` back-channel will fire
    it after core-worker PATCHes the row. Firing here would either
    crash (no embedding to pass) or duplicate the back-channel firing."""
    ctx = _ctx(embedding=None)
    contradiction_spy = AsyncMock(return_value=None)
    reembed_spy = AsyncMock(return_value=None)
    with (
        patch(
            "core_api.services.contradiction_detector.detect_contradictions_async",
            new=contradiction_spy,
        ),
        patch(
            "core_api.pipeline.steps.write.schedule_background_tasks.process_entity_extraction",
            new=AsyncMock(),
        ),
        patch(
            "core_api.services.memory_service._schedule_enrich_or_inline",
            new=AsyncMock(),
        ),
        patch(
            "core_api.services.memory_service._schedule_embed_or_reembed",
            new=reembed_spy,
        ),
    ):
        await ScheduleBackgroundTasks().execute(ctx)

    contradiction_spy.assert_not_called()
    # And the re-embed shim is scheduled instead — preserves existing flow.
    reembed_spy.assert_called_once()


# ---------------------------------------------------------------------------
# Preserves existing fast-branch behaviour
# ---------------------------------------------------------------------------


async def test_fast_still_schedules_enrichment() -> None:
    """The pre-PR-3 enrichment scheduling is unchanged — fast mode
    always defers LLM enrichment (PR 2 contract), and this branch is
    what completes the deferral via the bus or in-process shim."""
    ctx = _ctx(embedding=[0.1] * VECTOR_DIM)
    enrich_spy = AsyncMock(return_value=None)
    with (
        patch(
            "core_api.services.memory_service._schedule_enrich_or_inline",
            new=enrich_spy,
        ),
        patch(
            "core_api.pipeline.steps.write.schedule_background_tasks.process_entity_extraction",
            new=AsyncMock(),
        ),
        patch(
            "core_api.services.contradiction_detector.detect_contradictions_async",
            new=AsyncMock(),
        ),
    ):
        await ScheduleBackgroundTasks().execute(ctx)

    enrich_spy.assert_called_once()


async def test_fast_skips_enrichment_when_disabled() -> None:
    """Tenant-level enrichment kill-switch must still skip scheduling."""
    ctx = _ctx(embedding=[0.1] * VECTOR_DIM, enrichment=False)
    enrich_spy = AsyncMock(return_value=None)
    with (
        patch(
            "core_api.services.memory_service._schedule_enrich_or_inline",
            new=enrich_spy,
        ),
        patch(
            "core_api.pipeline.steps.write.schedule_background_tasks.process_entity_extraction",
            new=AsyncMock(),
        ),
        patch(
            "core_api.services.contradiction_detector.detect_contradictions_async",
            new=AsyncMock(),
        ),
    ):
        await ScheduleBackgroundTasks().execute(ctx)

    enrich_spy.assert_not_called()
