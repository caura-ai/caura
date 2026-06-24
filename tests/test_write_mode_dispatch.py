"""write_mode-aware deferral in ParallelEmbedEnrich.

Restores the original CAURA-229 contract that CAURA-524 +
CAURA-595 PR-C inadvertently flattened: strong mode forces inline
embed+enrich regardless of global flags; fast mode always defers LLM
enrichment regardless of global flags. See the module docstring of
``parallel_embed_enrich.py`` for the full contract.

These tests are the regression guard. If a future refactor breaks the
mode dispatch (e.g. by re-flattening through the global flags), they
fail loudly. They are the characterization tests neither CAURA-524 nor
CAURA-595 had — without them, the silent behaviour drift went
undetected for ~3 weeks in OSS and indefinitely in prod.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core_api.constants import VECTOR_DIM
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.write.parallel_embed_enrich import ParallelEmbedEnrich
from core_api.schemas import MemoryCreate

pytestmark = pytest.mark.asyncio


TENANT_ID = f"test-write-mode-dispatch-{uuid.uuid4().hex[:8]}"


def _input(
    content: str = "A test memory long enough to pass any content-length gate.",
) -> MemoryCreate:
    return MemoryCreate(
        tenant_id=TENANT_ID,
        fleet_id="f",
        agent_id="a",
        content=content,
        persist=True,
        entity_links=[],
    )


def _tenant_config() -> SimpleNamespace:
    return SimpleNamespace(
        enrichment_enabled=True,
        enrichment_provider="fake",
    )


def _ctx(*, mode: str | None) -> PipelineContext:
    """Build a context with the given resolved_write_mode."""
    data: dict = {"input": _input(), "content_hash": "f" * 64}
    if mode is not None:
        data["resolved_write_mode"] = mode
    return PipelineContext(data=data, tenant_config=_tenant_config())


# ---------------------------------------------------------------------------
# strong mode — embed + enrich ALWAYS inline, regardless of global flags
# ---------------------------------------------------------------------------


async def test_strong_runs_inline_when_both_flags_off() -> None:
    """The SaaS profile: both global flags False. Strong must still
    embed + enrich inline so CheckSemanticDuplicate gets a real
    embedding and the agent reads its enriched fields back.

    Pre-this-PR: strong silently inherited both deferrals → embedding
    NULL → dedup step short-circuited via the `embedding is None`
    guard → near-duplicate writes that should 409 were silently
    committed."""
    ctx = _ctx(mode="strong")
    enrichment_result = SimpleNamespace(retrieval_hint="")
    embed_value = [0.7] * VECTOR_DIM

    with (
        # F3 Phase 2: parallel_embed_enrich reads `settings.deployment_mode`
        # via the inline_embedding / inline_enrichment helpers. The
        # canonical (F, F) legacy state derives to "deferred".
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.deployment_mode",
            "deferred",
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.get_embedding",
            new=AsyncMock(return_value=embed_value),
        ) as embed,
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=enrichment_result),
        ) as enrich,
    ):
        await ParallelEmbedEnrich().execute(ctx)

    embed.assert_called_once()
    enrich.assert_called_once()
    assert ctx.data["embedding"] == embed_value
    assert ctx.data["enrichment"] is enrichment_result


async def test_strong_runs_inline_when_both_flags_on() -> None:
    """OSS local default. Same expectation — strong always inline."""
    ctx = _ctx(mode="strong")
    enrichment_result = SimpleNamespace(retrieval_hint="")
    embed_value = [0.8] * VECTOR_DIM

    with (
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.deployment_mode",
            "inline",
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.get_embedding",
            new=AsyncMock(return_value=embed_value),
        ) as embed,
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=enrichment_result),
        ) as enrich,
    ):
        await ParallelEmbedEnrich().execute(ctx)

    embed.assert_called_once()
    enrich.assert_called_once()
    assert ctx.data["embedding"] == embed_value
    assert ctx.data["enrichment"] is enrichment_result


# ---------------------------------------------------------------------------
# fast mode — LLM enrich ALWAYS deferred, regardless of global flag
# ---------------------------------------------------------------------------


async def test_fast_defers_enrichment_when_flag_on() -> None:
    """The OSS local default + fast mode: enrich_on_hot_path=True
    used to drag the LLM call into the request path. CAURA-229's
    fast contract was "no LLM on the request path". This PR restores
    that: fast mode always defers enrichment regardless of the flag.
    Embedding still runs inline because embed_on_hot_path=True."""
    ctx = _ctx(mode="fast")
    embed_value = [0.5] * VECTOR_DIM

    with (
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.deployment_mode",
            "inline",
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.get_embedding",
            new=AsyncMock(return_value=embed_value),
        ) as embed,
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(),
        ) as enrich,
    ):
        await ParallelEmbedEnrich().execute(ctx)

    embed.assert_called_once()
    enrich.assert_not_called()
    assert ctx.data["embedding"] == embed_value
    assert ctx.data["enrichment"] is None


async def test_fast_defers_both_when_both_flags_off() -> None:
    """SaaS prod: fast + both flags False. Both deferred, response
    returns in ~10ms with row pointing core-worker for backfill."""
    ctx = _ctx(mode="fast")

    with (
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.deployment_mode",
            "deferred",
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.get_embedding",
            new=AsyncMock(),
        ) as embed,
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(),
        ) as enrich,
    ):
        await ParallelEmbedEnrich().execute(ctx)

    embed.assert_not_called()
    enrich.assert_not_called()
    assert ctx.data["embedding"] is None
    assert ctx.data["enrichment"] is None


# ---------------------------------------------------------------------------
# no mode set (e.g. enrichment-only sub-pipeline) — falls back to global flags
# ---------------------------------------------------------------------------


async def test_no_mode_honors_flags_inline() -> None:
    """The extract-only / auto-chunk preamble runs build_enrichment_pipeline
    directly without going through _resolve_write_mode, so
    resolved_write_mode is unset. Behaviour must match the pre-PR
    global-flag-only logic in that case."""
    ctx = _ctx(mode=None)
    enrichment_result = SimpleNamespace(retrieval_hint="")
    embed_value = [0.3] * VECTOR_DIM

    with (
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.deployment_mode",
            "inline",
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.get_embedding",
            new=AsyncMock(return_value=embed_value),
        ) as embed,
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(return_value=enrichment_result),
        ) as enrich,
    ):
        await ParallelEmbedEnrich().execute(ctx)

    embed.assert_called_once()
    enrich.assert_called_once()
    assert ctx.data["embedding"] == embed_value
    assert ctx.data["enrichment"] is enrichment_result


async def test_no_mode_honors_flags_deferred() -> None:
    """SaaS-flags + no mode (sub-pipeline path) → both deferred,
    matching the pre-PR behaviour."""
    ctx = _ctx(mode=None)

    with (
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.deployment_mode",
            "deferred",
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.get_embedding",
            new=AsyncMock(),
        ) as embed,
        patch(
            "core_api.services.memory_enrichment.enrich_memory",
            new=AsyncMock(),
        ) as enrich,
    ):
        await ParallelEmbedEnrich().execute(ctx)

    embed.assert_not_called()
    enrich.assert_not_called()
    assert ctx.data["embedding"] is None
    assert ctx.data["enrichment"] is None
