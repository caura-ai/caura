"""Integration tests: pipeline path vs legacy path produce equivalent output.

These tests require a running PostgreSQL instance (same as other integration tests).
They exercise both paths with identical inputs and compare the MemoryOut results.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from common.models.memory import Memory
from core_api.constants import VECTOR_DIM
from core_api.schemas import MemoryCreate, MemoryOut

# Ensure pipeline flag is off for legacy path tests
# (individual tests toggle it as needed)

TENANT_ID = f"test-pipeline-{uuid.uuid4().hex[:8]}"
FLEET_ID = "test-fleet"
AGENT_ID = "test-agent"


def _make_input(
    content: str = "This is a test memory with enough content to pass the quality gate for the pipeline test suite.",
    persist: bool = True,
    **kwargs,
) -> MemoryCreate:
    return MemoryCreate(
        tenant_id=TENANT_ID,
        fleet_id=FLEET_ID,
        agent_id=AGENT_ID,
        content=content,
        persist=persist,
        entity_links=[],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Unit tests (no DB required)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_step_rejects_short_content():
    """CheckContentLength raises 422 for content below minimum length."""
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.write.check_content_length import CheckContentLength

    ctx = PipelineContext(
                data={"input": _make_input(content="hi")},
    )
    step = CheckContentLength()

    with pytest.raises(HTTPException) as exc_info:
        await step.execute(ctx)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_compute_content_hash_skips_cache_lookup_when_embedding_deferred(
    monkeypatch,
):
    """CAURA-682 / CAURA-685: ComputeContentHash must NOT call
    ``sc.find_embedding_by_content_hash`` when ``settings.inline_embedding``
    is False (the SaaS / staging default). The cache lookup is only
    useful to bypass an inline ``get_embedding`` call; when embedding is
    deferred to ``core-worker``, the lookup is wasted work AND under
    storm load (noisy-neighbor scenario) it observed p95 of ~17.8 s on
    staging — the single step that dominated the write degradation."""
    from unittest.mock import AsyncMock, patch
    from core_api.config import settings
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.write.compute_content_hash import ComputeContentHash

    # ``inline_embedding`` is a derived property from ``deployment_mode``.
    monkeypatch.setattr(settings, "deployment_mode", "deferred")
    ctx = PipelineContext(data={"input": _make_input()})

    mock_sc = AsyncMock()
    with patch(
        "core_api.pipeline.steps.write.compute_content_hash.get_storage_client",
        return_value=mock_sc,
    ):
        await ComputeContentHash().execute(ctx)

    mock_sc.find_embedding_by_content_hash.assert_not_called()
    # Hash is still computed; just the lookup is skipped.
    assert ctx.data["content_hash"] is not None
    assert ctx.data["cached_embedding"] is None


@pytest.mark.asyncio
async def test_compute_content_hash_still_looks_up_when_embedding_inline(
    monkeypatch,
):
    """OSS-standalone (``inline_embedding=True``) preserves the optimization
    — the cache lookup still runs so a duplicate-content write skips the
    inline ``get_embedding`` call. Regression guard for the path the
    deferred-mode fix MUST NOT break."""
    from unittest.mock import AsyncMock, patch
    from core_api.config import settings
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.write.compute_content_hash import ComputeContentHash

    monkeypatch.setattr(settings, "deployment_mode", "inline")
    ctx = PipelineContext(data={"input": _make_input()})

    mock_sc = AsyncMock()
    mock_sc.find_embedding_by_content_hash.return_value = [0.1, 0.2, 0.3]
    with patch(
        "core_api.pipeline.steps.write.compute_content_hash.get_storage_client",
        return_value=mock_sc,
    ):
        await ComputeContentHash().execute(ctx)

    mock_sc.find_embedding_by_content_hash.assert_called_once()
    assert ctx.data["cached_embedding"] == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_check_exact_duplicate_records_dedup_lookup_ms():
    """CheckExactDuplicate times just its storage roundtrip into
    ``phase_timings["dedup_lookup_ms"]`` so the ``memory_write_latency``
    emit can separate the dedup lookup (GET /by-content-hash) from the
    insert (``storage_ms``) and from core-api-side overhead — the split
    that attributes the single_write p99 tail."""
    from unittest.mock import AsyncMock, patch
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.write.check_exact_duplicate import CheckExactDuplicate

    ctx = PipelineContext(
                data={"input": _make_input(), "content_hash": "deadbeef"},
    )
    mock_sc = AsyncMock()
    mock_sc.find_by_content_hash.return_value = None  # no duplicate
    with patch(
        "core_api.pipeline.steps.write.check_exact_duplicate.get_storage_client",
        return_value=mock_sc,
    ):
        result = await CheckExactDuplicate().execute(ctx)

    assert result is None
    mock_sc.find_by_content_hash.assert_called_once()
    dedup_ms = ctx.data["phase_timings"]["dedup_lookup_ms"]
    assert isinstance(dedup_ms, int) and dedup_ms >= 0


@pytest.mark.asyncio
async def test_apply_enrichment_step_defaults():
    """MergeEnrichmentFields applies correct defaults when no enrichment."""
    from unittest.mock import AsyncMock
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.write.merge_enrichment_fields import (
        MergeEnrichmentFields,
    )

    ctx = PipelineContext(
                data={
            "input": _make_input(),
            "enrichment": None,
        },
    )
    step = MergeEnrichmentFields()
    await step.execute(ctx)

    fields = ctx.data["memory_fields"]
    assert fields["memory_type"] == "fact"
    assert fields["weight"] == 0.5  # DEFAULT_MEMORY_WEIGHT
    assert fields["status"] == "active"
    assert fields["title"] is None


# ---------------------------------------------------------------------------
# Integration tests (require PostgreSQL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_path_creates_memory(db):
    """Pipeline path creates a memory and returns valid MemoryOut."""
    from core_api.services import memory_service

    # Temporarily enable pipeline
    original = memory_service._USE_PIPELINE_WRITE
    memory_service._USE_PIPELINE_WRITE = True
    try:
        from core_api.services.memory_service import create_memory

        data = _make_input()
        result = await create_memory(data)

        assert isinstance(result, MemoryOut)
        assert result.tenant_id == TENANT_ID
        assert result.content == data.content
        assert result.memory_type is not None
        assert result.status is not None
    finally:
        memory_service._USE_PIPELINE_WRITE = original


@pytest.mark.asyncio
async def test_pipeline_emits_memory_write_latency_log(db, caplog):
    """CAURA-682 Phase 1: write pipeline emits ``memory_write_latency``
    structured log with per-phase timings + tenant_id at INFO level.

    The summary log is the input to GCP-side per-tenant slice queries that
    answer "which phase is the bottleneck under noisy-neighbor load?" —
    so the test pins the field surface contract, not specific values.
    """
    import logging

    from core_api.services import memory_service

    original = memory_service._USE_PIPELINE_WRITE
    memory_service._USE_PIPELINE_WRITE = True
    try:
        from core_api.services.memory_service import create_memory

        data = _make_input(
            content="CAURA-682 Phase 1 latency log emission test — unique content for hash dedup."
        )
        with caplog.at_level(logging.INFO, logger="core_api.services.memory_service"):
            await create_memory(data)

        latency_records = [
            r for r in caplog.records if r.getMessage() == "memory_write_latency"
        ]
        assert len(latency_records) == 1, (
            f"expected exactly one memory_write_latency log, got {len(latency_records)}"
        )
        record = latency_records[0]
        # Pin the field surface so the GCP log query stays stable.
        for key in (
            "path",
            "tenant_id",
            "agent_id",
            "fleet_id",
            "write_mode",
            "total_ms",
            "embedding_pending",
            "enrichment_pending",
            "cached_embedding",
        ):
            assert hasattr(record, key), f"missing field: {key}"
        assert record.path == "memory-write"
        assert record.tenant_id == TENANT_ID
        assert record.agent_id == AGENT_ID
        assert record.fleet_id == FLEET_ID
        assert record.write_mode in ("fast", "strong")
        assert isinstance(record.total_ms, int)
        assert record.total_ms >= 0
        # Storage call always runs; per-phase keys present when the
        # phase ran inline (may be None if deferred to core-worker).
        assert hasattr(record, "storage_ms")
        assert hasattr(record, "entity_links_ms")
        assert hasattr(record, "embedding_ms")
        assert hasattr(record, "enrichment_ms")
        # Success path: ``success`` must be True so failed-vs-successful
        # write filters in GCP work.
        assert record.success is True
    finally:
        memory_service._USE_PIPELINE_WRITE = original


@pytest.mark.asyncio
async def test_pipeline_emits_latency_log_on_pipeline_failure(db, caplog):
    """CAURA-682 Phase 1: timeouts (the actual noisy-neighbor failure mode)
    must also produce a ``memory_write_latency`` log with ``success=False``
    and whatever partial timings landed before the exception.

    Pre-fix, the log lived after ``await pipeline.run(ctx)`` with no
    exception handling, so a ``HTTPException(504)`` from
    ``parallel_embed_enrich`` on ``asyncio.wait_for`` timeout skipped the
    log entirely — the worst case for diagnosis."""
    import logging
    from unittest.mock import patch

    from fastapi import HTTPException
    from core_api.pipeline.runner import Pipeline
    from core_api.services import memory_service

    original = memory_service._USE_PIPELINE_WRITE
    memory_service._USE_PIPELINE_WRITE = True

    async def _boom(self, ctx):
        # Mirrors parallel_embed_enrich's timeout shape exactly.
        raise HTTPException(
            status_code=504, detail="Memory write timed out (embedding/enrichment)"
        )

    try:
        from core_api.services.memory_service import create_memory

        data = _make_input(
            content="CAURA-682 Phase 1 timeout-emits-log path — distinct content for hash uniqueness."
        )
        with (
            caplog.at_level(logging.INFO, logger="core_api.services.memory_service"),
            patch.object(Pipeline, "run", _boom),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await create_memory(data)
            assert exc_info.value.status_code == 504

        latency_records = [
            r for r in caplog.records if r.getMessage() == "memory_write_latency"
        ]
        assert len(latency_records) == 1, (
            f"expected exactly one memory_write_latency log on failure, got {len(latency_records)}"
        )
        record = latency_records[0]
        assert record.success is False
        assert record.tenant_id == TENANT_ID
        # ``total_ms`` is computed in finally → must be present even when
        # the pipeline raised before populating ``ctx.data["memory"]``.
        assert isinstance(record.total_ms, int)
        assert record.embedding_pending is False
        assert record.enrichment_pending is False
    finally:
        memory_service._USE_PIPELINE_WRITE = original


@pytest.mark.asyncio
async def test_legacy_path_creates_memory(db):
    """Legacy path creates a memory and returns valid MemoryOut (baseline)."""
    from core_api.services import memory_service

    original = memory_service._USE_PIPELINE_WRITE
    memory_service._USE_PIPELINE_WRITE = False
    try:
        from core_api.services.memory_service import create_memory

        data = _make_input(
            content="Legacy path baseline test memory — unique content to avoid hash collision with pipeline path test."
        )
        result = await create_memory(data)

        assert isinstance(result, MemoryOut)
        assert result.tenant_id == TENANT_ID
        assert result.content == data.content
    finally:
        memory_service._USE_PIPELINE_WRITE = original


@pytest.mark.asyncio
async def test_pipeline_extract_only(db):
    """Pipeline path returns preview MemoryOut when persist=False."""
    from core_api.services import memory_service

    original = memory_service._USE_PIPELINE_WRITE
    memory_service._USE_PIPELINE_WRITE = True
    try:
        from core_api.services.memory_service import create_memory

        extract_content = f"Pipeline extract-only test memory — unique {uuid.uuid4().hex[:8]} — should NOT be persisted."
        data = _make_input(content=extract_content, persist=False)
        result = await create_memory(data)

        assert isinstance(result, MemoryOut)
        assert result.content == data.content
        # Extract-only should not persist — verify no row in DB
        stmt = select(Memory).where(
            Memory.tenant_id == TENANT_ID,
            Memory.content == extract_content,
        )
        rows = (await db.execute(stmt)).scalars().all()
        assert len(rows) == 0
    finally:
        memory_service._USE_PIPELINE_WRITE = original


@pytest.mark.asyncio
async def test_pipeline_hash_dedup(db):
    """Pipeline path raises 409 on duplicate content_hash."""
    from fastapi import HTTPException
    from core_api.services import memory_service

    original = memory_service._USE_PIPELINE_WRITE
    memory_service._USE_PIPELINE_WRITE = True
    try:
        from core_api.services.memory_service import create_memory

        data = _make_input(
            content="Unique content for hash dedup test in pipeline write refactor."
        )
        await create_memory(data)

        # Second write with same content should 409
        with pytest.raises(HTTPException) as exc_info:
            await create_memory(data)
        assert exc_info.value.status_code == 409
    finally:
        memory_service._USE_PIPELINE_WRITE = original


@pytest.mark.asyncio
async def test_pipeline_quality_gate(db):
    """Pipeline path rejects short content with 422."""
    from fastapi import HTTPException
    from core_api.services import memory_service

    original = memory_service._USE_PIPELINE_WRITE
    memory_service._USE_PIPELINE_WRITE = True
    try:
        from core_api.services.memory_service import create_memory

        data = _make_input(content="hi")
        with pytest.raises(HTTPException) as exc_info:
            await create_memory(data)
        assert exc_info.value.status_code == 422
    finally:
        memory_service._USE_PIPELINE_WRITE = original


@pytest.mark.asyncio
async def test_pipeline_equivalence(db):
    """Pipeline and legacy paths produce equivalent MemoryOut fields."""
    from core_api.services import memory_service
    from core_api.services.memory_service import (
        _create_memory_legacy,
        _create_memory_pipeline,
    )

    content_a = "Pipeline equivalence test memory content A — testing that both paths produce the same output fields."
    content_b = "Pipeline equivalence test memory content B — testing that both paths produce the same output fields."

    # Legacy path
    memory_service._USE_PIPELINE_WRITE = False
    data_legacy = _make_input(content=content_a)
    result_legacy = await _create_memory_legacy(data_legacy)

    # Pipeline path
    memory_service._USE_PIPELINE_WRITE = True
    data_pipeline = _make_input(content=content_b)
    result_pipeline = await _create_memory_pipeline(data_pipeline)

    # Reset
    memory_service._USE_PIPELINE_WRITE = False

    # Compare key fields (IDs and timestamps will differ)
    assert result_legacy.tenant_id == result_pipeline.tenant_id
    assert result_legacy.fleet_id == result_pipeline.fleet_id
    assert result_legacy.agent_id == result_pipeline.agent_id
    assert result_legacy.memory_type == result_pipeline.memory_type
    assert result_legacy.weight == result_pipeline.weight
    assert result_legacy.status == result_pipeline.status
    assert result_legacy.visibility == result_pipeline.visibility


# ---------------------------------------------------------------------------
# Write-mode dial unit tests (no DB required)
# ---------------------------------------------------------------------------


class TestResolveWriteMode:
    """Unit tests for _resolve_write_mode pure function."""

    def _make_config(self, default_write_mode: str = "fast"):
        """Create a minimal mock tenant config."""
        cfg = type("Cfg", (), {"default_write_mode": default_write_mode})()
        return cfg

    def test_explicit_fast(self):
        from core_api.services.memory_service import _resolve_write_mode

        data = _make_input(write_mode="fast")
        assert _resolve_write_mode(data, self._make_config("strong")) == "fast"

    def test_explicit_strong(self):
        from core_api.services.memory_service import _resolve_write_mode

        data = _make_input(write_mode="strong")
        assert _resolve_write_mode(data, self._make_config("fast")) == "strong"

    def test_auto_generic_type_uses_tenant_default(self):
        from core_api.services.memory_service import _resolve_write_mode

        data = _make_input(write_mode="auto", memory_type="fact")
        assert _resolve_write_mode(data, self._make_config("fast")) == "fast"

    def test_auto_decision_forces_strong(self):
        from core_api.services.memory_service import _resolve_write_mode

        data = _make_input(write_mode="auto", memory_type="decision")
        assert _resolve_write_mode(data, self._make_config("fast")) == "strong"

    def test_auto_commitment_forces_strong(self):
        from core_api.services.memory_service import _resolve_write_mode

        data = _make_input(write_mode="auto", memory_type="commitment")
        assert _resolve_write_mode(data, self._make_config("fast")) == "strong"

    def test_auto_cancellation_forces_strong(self):
        from core_api.services.memory_service import _resolve_write_mode

        data = _make_input(write_mode="auto", memory_type="cancellation")
        assert _resolve_write_mode(data, self._make_config("fast")) == "strong"

    def test_none_mode_uses_tenant_default(self):
        from core_api.services.memory_service import _resolve_write_mode

        data = _make_input()  # write_mode=None
        assert _resolve_write_mode(data, self._make_config("strong")) == "strong"

    def test_none_mode_none_type_uses_tenant_default(self):
        from core_api.services.memory_service import _resolve_write_mode

        data = _make_input()  # write_mode=None, memory_type=None
        assert _resolve_write_mode(data, self._make_config("fast")) == "fast"


@pytest.mark.asyncio
async def test_merge_enrichment_sets_pending_in_fast_mode():
    """MergeEnrichmentFields sets enrichment_pending=True in fast mode with no enrichment."""
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.write.merge_enrichment_fields import (
        MergeEnrichmentFields,
    )

    ctx = PipelineContext(
                data={
            "input": _make_input(),
            "enrichment": None,
            "resolved_write_mode": "fast",
        },
    )
    step = MergeEnrichmentFields()
    await step.execute(ctx)

    fields = ctx.data["memory_fields"]
    assert fields["metadata"]["enrichment_pending"] is True
    assert fields["metadata"]["write_mode"] == "fast"


@pytest.mark.asyncio
async def test_merge_enrichment_no_pending_in_strong_mode():
    """MergeEnrichmentFields does NOT set enrichment_pending in strong mode."""
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.write.merge_enrichment_fields import (
        MergeEnrichmentFields,
    )

    ctx = PipelineContext(
                data={
            "input": _make_input(),
            "enrichment": None,
            "resolved_write_mode": "strong",
        },
    )
    step = MergeEnrichmentFields()
    await step.execute(ctx)

    fields = ctx.data["memory_fields"]
    assert "enrichment_pending" not in fields["metadata"]
    assert fields["metadata"]["write_mode"] == "strong"


@pytest.mark.asyncio
async def test_schedule_background_tasks_fast_mode_fires_full_fan_out():
    """ScheduleBackgroundTasks fast branch fires background enrichment +
    entity extraction + Path A contradiction detection.

    Pre-Gap-01/04 fix: fast mode only fired background_enrichment and
    relied on ``_enrich_memory_background`` to chain into the others —
    a chain that didn't fire in every flag profile (Enterprise+fast lost
    entity extraction, OSS+fast lost Path A). The fast branch now fires
    each directly, mirroring the strong branch's symmetric fan-out."""
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.write.schedule_background_tasks import (
        ScheduleBackgroundTasks,
    )

    mock_memory = type("M", (), {"id": uuid.uuid4()})()
    mock_config = type(
        "C",
        (),
        {
            "enrichment_enabled": True,
            "entity_extraction_enabled": True,
        },
    )()

    ctx = PipelineContext(
                data={
            "input": _make_input(),
            "memory": mock_memory,
            "embedding": [0.1] * VECTOR_DIM,
            "resolved_write_mode": "fast",
        },
        tenant_config=mock_config,
    )

    with patch(
        "core_api.pipeline.steps.write.schedule_background_tasks.track_task"
    ) as mock_track:
        step = ScheduleBackgroundTasks()
        await step.execute(ctx)

    # 3 tracked tasks: background_enrichment + entity_extraction + Path A.
    # Embed-reembed is NOT scheduled here because embedding is present.
    # Detailed per-task wiring is covered in tests/test_fast_branch_fan_out.py.
    assert mock_track.call_count == 3


@pytest.mark.asyncio
async def test_schedule_background_tasks_strong_mode_fires_entity_and_contradiction():
    """ScheduleBackgroundTasks fires entity extraction + contradiction in strong mode."""
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.write.schedule_background_tasks import (
        ScheduleBackgroundTasks,
    )

    mock_memory = type("M", (), {"id": uuid.uuid4()})()
    mock_config = type(
        "C",
        (),
        {
            "enrichment_enabled": True,
            "entity_extraction_enabled": True,
        },
    )()

    ctx = PipelineContext(
                data={
            "input": _make_input(),
            "memory": mock_memory,
            "embedding": [0.1] * VECTOR_DIM,
            "resolved_write_mode": "strong",
        },
        tenant_config=mock_config,
    )

    with patch(
        "core_api.pipeline.steps.write.schedule_background_tasks.track_task"
    ) as mock_track:
        step = ScheduleBackgroundTasks()
        await step.execute(ctx)

    # Should fire entity_extraction + contradiction_detection
    assert mock_track.call_count == 2


# ---------------------------------------------------------------------------
# Write-mode dial integration tests (require PostgreSQL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_mode_creates_memory_with_pending_enrichment(db):
    """Fast mode creates memory with enrichment_pending=True and write_mode=fast."""
    from core_api.services import memory_service

    original = memory_service._USE_PIPELINE_WRITE
    memory_service._USE_PIPELINE_WRITE = True
    try:
        from core_api.services.memory_service import create_memory

        data = _make_input(
            content="Fast mode test memory — should store quickly with deferred enrichment for the write mode dial.",
            write_mode="fast",
        )
        result = await create_memory(data)

        assert isinstance(result, MemoryOut)
        assert result.tenant_id == TENANT_ID
        assert result.metadata is not None
        assert result.metadata.get("enrichment_pending") is True
        assert result.metadata.get("write_mode") == "fast"
    finally:
        memory_service._USE_PIPELINE_WRITE = original


@pytest.mark.asyncio
async def test_strong_mode_creates_memory_same_as_today(db):
    """Strong mode produces same result as today's pipeline (full enrichment inline)."""
    from core_api.services import memory_service

    original = memory_service._USE_PIPELINE_WRITE
    memory_service._USE_PIPELINE_WRITE = True
    try:
        from core_api.services.memory_service import create_memory

        data = _make_input(
            content="Strong mode test memory — should run full enrichment inline for the write mode dial test.",
            write_mode="strong",
        )
        result = await create_memory(data)

        assert isinstance(result, MemoryOut)
        assert result.metadata is not None
        assert result.metadata.get("write_mode") == "strong"
        # Strong mode should NOT have enrichment_pending
        assert result.metadata.get("enrichment_pending") is None
    finally:
        memory_service._USE_PIPELINE_WRITE = original


@pytest.mark.asyncio
async def test_auto_mode_decision_type_routes_to_strong(db):
    """Auto mode with memory_type=decision routes to strong path."""
    from core_api.services import memory_service

    original = memory_service._USE_PIPELINE_WRITE
    memory_service._USE_PIPELINE_WRITE = True
    try:
        from core_api.services.memory_service import create_memory

        data = _make_input(
            content="We decided to use PostgreSQL for the primary database — auto mode should route this to strong path.",
            write_mode="auto",
            memory_type="decision",
        )
        result = await create_memory(data)

        assert isinstance(result, MemoryOut)
        assert result.metadata is not None
        assert result.metadata.get("write_mode") == "strong"
    finally:
        memory_service._USE_PIPELINE_WRITE = original
