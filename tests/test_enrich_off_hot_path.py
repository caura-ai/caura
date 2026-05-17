"""CAURA-595 — LLM enrichment off the memory-write hot path.

Mirrors ``test_embed_off_hot_path`` for the enrichment flag. Two paths
exercised:

* **flag=False (CAURA-595 SaaS):** ``ParallelEmbedEnrich`` skips the
  inline ``enrich_memory`` call and ``ScheduleBackgroundTasks`` publishes
  ``Topics.Memory.ENRICH_REQUESTED``. ``_schedule_enrich_or_inline`` is
  the shim that picks the bus.
* **flag=True (default):** the enrichment call runs inline; existing
  in-process ``_enrich_memory_background`` continues to handle the fast-
  mode write path.

Worker-side ``handle_enrich_request`` is covered by
``core-worker/tests/test_consumer_enrich.py``; this file focuses on
core-api's gate logic and the ``ENRICHED`` back-channel publish from
the worker.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.constants import VECTOR_DIM
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.write.parallel_embed_enrich import ParallelEmbedEnrich
from core_api.pipeline.steps.write.schedule_background_tasks import (
    ScheduleBackgroundTasks,
)
from core_api.schemas import MemoryCreate

pytestmark = pytest.mark.asyncio


TENANT_ID = f"test-595-{uuid.uuid4().hex[:8]}"


def _input(**overrides) -> MemoryCreate:
    base = {
        "tenant_id": TENANT_ID,
        "fleet_id": "f",
        "agent_id": "a",
        "content": "A test memory long enough to pass any content-length gate.",
        "persist": True,
        "entity_links": [],
    }
    base.update(overrides)
    return MemoryCreate(**base)


def _tenant_config(*, enrichment: bool = True) -> SimpleNamespace:
    """Duck-typed tenant config — only attributes the steps under test read."""
    return SimpleNamespace(
        enrichment_enabled=enrichment,
        enrichment_provider="openai" if enrichment else "none",
        enrichment_model=None,
        entity_extraction_enabled=False,
        openai_api_key="sk-tenant",
        anthropic_api_key=None,
        openrouter_api_key=None,
        gemini_api_key=None,
        resolve_fallback=lambda: (None, None),
    )


def _ctx(
    *,
    enrichment: bool = True,
    cached_embedding=None,
    memory_id: uuid.UUID | None = None,
    write_mode: str | None = None,
    data: MemoryCreate | None = None,
) -> PipelineContext:
    # Default ``write_mode=None`` exercises the extract-only / auto-chunk
    # sub-pipeline path where ``ParallelEmbedEnrich`` falls back to the
    # global ``enrich_on_hot_path`` flag. Tests that need the explicit
    # fast/strong dispatch must pass ``write_mode`` explicitly; the
    # mode-aware override behaviour is covered in
    # ``tests/test_write_mode_dispatch.py``.
    ctx_data: dict = {
        "input": data or _input(),
        "content_hash": "f" * 64,
        "memory": {"id": memory_id or uuid.uuid4()},
        "embedding": [0.1] * VECTOR_DIM,
        "resolved_write_mode": write_mode,
    }
    if cached_embedding is not None:
        ctx_data["cached_embedding"] = cached_embedding
    return PipelineContext(
        db=AsyncMock(),
        data=ctx_data,
        tenant_config=_tenant_config(enrichment=enrichment),
    )


# ---------------------------------------------------------------------------
# ParallelEmbedEnrich
# ---------------------------------------------------------------------------


async def test_skips_inline_enrich_when_flag_off() -> None:
    """``enrich_on_hot_path=False`` skips the LLM call entirely; the
    pipeline records ``enrichment=None`` and the strong-write response
    drops the LLM-derived fields."""
    ctx = _ctx(enrichment=True)
    enrich_spy = AsyncMock(return_value=SimpleNamespace(retrieval_hint=""))
    with (
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.enrich_on_hot_path",
            False,
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.get_embedding",
            new=AsyncMock(return_value=[0.0] * VECTOR_DIM),
        ),
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=enrich_spy,
        ),
    ):
        await ParallelEmbedEnrich().execute(ctx)
    enrich_spy.assert_not_called()
    assert ctx.data["enrichment"] is None


async def test_runs_inline_enrich_when_flag_on() -> None:
    """OSS default: enrichment runs inline alongside embed."""
    ctx = _ctx(enrichment=True)
    enrichment_result = SimpleNamespace(retrieval_hint="")

    async def _enrich(*_a, **_k):
        return enrichment_result

    with (
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.enrich_on_hot_path",
            True,
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.get_embedding",
            new=AsyncMock(return_value=[0.1] * VECTOR_DIM),
        ),
        patch("core_api.services.memory_enrichment.enrich_memory", new=_enrich),
    ):
        await ParallelEmbedEnrich().execute(ctx)
    assert ctx.data["enrichment"] is enrichment_result


async def test_hint_reembed_skipped_when_enrich_flag_off() -> None:
    """Hint re-embed needs the inline enrichment output. Deferring enrich
    means the hint isn't available; back-channel ENRICHED consumer can
    pick it up later if high-value."""
    ctx = _ctx(enrichment=True)
    embed_spy = AsyncMock(return_value=[0.1] * VECTOR_DIM)
    with (
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.enrich_on_hot_path",
            False,
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.get_embedding",
            new=embed_spy,
        ),
        patch("core_api.services.memory_enrichment.enrich_memory", new=AsyncMock()),
    ):
        await ParallelEmbedEnrich().execute(ctx)
    # Single embed call (initial), no follow-up hint re-embed.
    assert embed_spy.await_count == 1


# ---------------------------------------------------------------------------
# ScheduleBackgroundTasks — strong-mode publish branch
# ---------------------------------------------------------------------------


async def test_strong_mode_publishes_enrich_when_flag_off() -> None:
    """Strong-write + flag=off triggers ``_schedule_enrich_or_inline``."""
    memory_id = uuid.uuid4()
    ctx = _ctx(memory_id=memory_id, write_mode="strong")
    ctx.data["enrichment"] = None  # parallel step deferred

    publish_spy = AsyncMock(return_value=None)
    with (
        patch(
            "core_api.pipeline.steps.write.schedule_background_tasks.settings.enrich_on_hot_path",
            False,
        ),
        patch(
            "core_api.services.memory_service._schedule_enrich_or_inline",
            new=publish_spy,
        ),
        patch(
            "core_api.services.contradiction_detector.detect_contradictions_async",
            new=AsyncMock(),
        ),
    ):
        await ScheduleBackgroundTasks().execute(ctx)

    publish_spy.assert_called_once()
    call_args = publish_spy.call_args
    assert call_args.args[0] == memory_id
    assert call_args.args[2] == TENANT_ID


async def test_strong_mode_no_publish_when_flag_on() -> None:
    """Strong-write + flag=on → enrichment ran inline, no scheduling needed."""
    memory_id = uuid.uuid4()
    ctx = _ctx(memory_id=memory_id, write_mode="strong")
    ctx.data["enrichment"] = SimpleNamespace(retrieval_hint="")  # ran inline

    publish_spy = AsyncMock()
    with (
        patch(
            "core_api.pipeline.steps.write.schedule_background_tasks.settings.enrich_on_hot_path",
            True,
        ),
        patch(
            "core_api.services.memory_service._schedule_enrich_or_inline",
            new=publish_spy,
        ),
        patch(
            "core_api.services.contradiction_detector.detect_contradictions_async",
            new=AsyncMock(),
        ),
    ):
        await ScheduleBackgroundTasks().execute(ctx)

    publish_spy.assert_not_called()


async def test_fast_mode_uses_schedule_enrich_helper() -> None:
    """Fast mode unconditionally enqueues background enrichment via the
    new helper; the helper picks publish vs in-process based on flag."""
    memory_id = uuid.uuid4()
    ctx = _ctx(memory_id=memory_id, write_mode="fast")

    helper_spy = AsyncMock(return_value=None)
    with (
        patch(
            "core_api.services.memory_service._schedule_enrich_or_inline",
            new=helper_spy,
        ),
        patch(
            "core_api.services.memory_service._schedule_embed_or_reembed",
            new=AsyncMock(),
        ),
    ):
        await ScheduleBackgroundTasks().execute(ctx)

    helper_spy.assert_called_once()


# ---------------------------------------------------------------------------
# _schedule_enrich_or_inline shim
# ---------------------------------------------------------------------------


async def test_schedule_enrich_publishes_when_flag_off() -> None:
    from core_api.services import memory_service

    memory_id = uuid.uuid4()
    publish_spy = AsyncMock(return_value=None)
    bg_spy = AsyncMock(return_value=None)
    tc = _tenant_config()

    with (
        patch.object(memory_service.settings, "enrich_on_hot_path", False),
        patch.object(memory_service, "publish_memory_enrich_request", new=publish_spy),
        patch.object(memory_service, "_enrich_memory_background", new=bg_spy),
    ):
        await memory_service._schedule_enrich_or_inline(
            memory_id,
            "content",
            TENANT_ID,
            "f",
            "a",
            tc,
            agent_provided_fields=["weight"],
        )

    publish_spy.assert_awaited_once()
    bg_spy.assert_not_called()
    kwargs = publish_spy.await_args.kwargs
    assert kwargs["memory_id"] == memory_id
    assert kwargs["tenant_id"] == TENANT_ID
    assert kwargs["agent_provided_fields"] == ["weight"]


async def test_schedule_enrich_runs_inline_when_flag_on() -> None:
    from core_api.services import memory_service

    memory_id = uuid.uuid4()
    publish_spy = AsyncMock()
    bg_spy = AsyncMock(return_value=None)
    tc = _tenant_config()

    with (
        patch.object(memory_service.settings, "enrich_on_hot_path", True),
        patch.object(memory_service, "publish_memory_enrich_request", new=publish_spy),
        patch.object(memory_service, "_enrich_memory_background", new=bg_spy),
    ):
        await memory_service._schedule_enrich_or_inline(
            memory_id,
            "content",
            TENANT_ID,
            "f",
            "a",
            tc,
        )

    publish_spy.assert_not_called()
    bg_spy.assert_awaited_once()


# ---------------------------------------------------------------------------
# _agent_provided_enrichment_fields
# ---------------------------------------------------------------------------


def test_agent_provided_fields_extracts_overlap_from_model_fields_set() -> None:
    """Only enrichment-relevant fields the agent set explicitly are returned."""
    from core_api.services.memory_service import _agent_provided_enrichment_fields

    data = MemoryCreate(
        tenant_id="t",
        agent_id="a",
        content="hello world",
        weight=0.9,
        memory_type="decision",
    )
    assert _agent_provided_enrichment_fields(data) == ["memory_type", "weight"]


def test_agent_provided_fields_ignores_unrelated_fields() -> None:
    """Fields the agent set that aren't enrichment-relevant don't leak into
    the gate (e.g. ``content``, ``tenant_id``)."""
    from core_api.services.memory_service import _agent_provided_enrichment_fields

    data = MemoryCreate(tenant_id="t", agent_id="a", content="x")
    # No enrichment-relevant fields set → None.
    assert _agent_provided_enrichment_fields(data) is None


def test_agent_provided_fields_returns_none_for_non_pydantic_input() -> None:
    """Synthetic dict-like input doesn't expose ``model_fields_set``;
    fall through to None so the helper is safe in test harnesses."""
    from core_api.services.memory_service import _agent_provided_enrichment_fields

    assert _agent_provided_enrichment_fields(MagicMock(spec=[])) is None
