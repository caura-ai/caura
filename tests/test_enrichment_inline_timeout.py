"""CAURA-000 — configurable inline embed+enrich timeout.

The 504 "Memory write timed out (embedding/enrichment)" was firing on
memclaw.dev under load because the gather in ParallelEmbedEnrich had a
hardcoded 20s ceiling, which is too tight once embedding moved off the
hot path (CAURA-594) and enrichment LLM became the sole occupant.

Verifies:
  - ``settings.enrichment_inline_timeout_seconds`` is the value passed
    to ``asyncio.wait_for`` (no longer the hardcoded 20.0).
  - The Settings model validator rejects ordering inversions that would
    let the inner cap miss the outer request budget.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core_api.config import Settings, settings as _settings_singleton
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.write.parallel_embed_enrich import ParallelEmbedEnrich
from core_api.schemas import MemoryCreate


TENANT_ID = f"test-inline-timeout-{uuid.uuid4().hex[:8]}"


def _input() -> MemoryCreate:
    return MemoryCreate(
        tenant_id=TENANT_ID,
        fleet_id="f",
        agent_id="a",
        content="content long enough to pass any length gate",
        persist=True,
        entity_links=[],
    )


def _ctx(*, enrichment: bool = True) -> PipelineContext:
    tenant_config = SimpleNamespace(
        enrichment_enabled=enrichment,
        enrichment_provider="fake" if enrichment else "none",
    )
    return PipelineContext(
        db=AsyncMock(),
        data={"input": _input(), "content_hash": "f" * 64},
        tenant_config=tenant_config,
    )


# ---------------------------------------------------------------------------
# Setting is wired into the wait_for ceiling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_timeout_uses_setting_value() -> None:
    """ParallelEmbedEnrich must read the timeout from settings, not a
    hardcoded constant. Captures the value via a patched wait_for."""
    captured: dict[str, float] = {}

    async def _spy_wait_for(awaitable, timeout):
        captured["timeout"] = timeout
        return await awaitable

    enrichment_result = SimpleNamespace(retrieval_hint="")

    async def _enrich(*_a, **_k):
        return enrichment_result

    with (
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.enrichment_inline_timeout_seconds",
            7.5,
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.embed_on_hot_path",
            False,
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.asyncio.wait_for",
            new=_spy_wait_for,
        ),
        patch("core_api.services.memory_enrichment.enrich_memory", new=_enrich),
    ):
        await ParallelEmbedEnrich().execute(_ctx(enrichment=True))

    assert captured["timeout"] == 7.5


@pytest.mark.asyncio
async def test_504_fires_when_inner_timeout_exceeded() -> None:
    """A slow enrichment must surface as a 504 with the configured detail
    message — verifies the timeout still fires (just at the new ceiling)."""
    from fastapi import HTTPException

    async def _slow_enrich(*_a, **_k):
        await asyncio.sleep(10)  # well past the tiny test ceiling

    with (
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.enrichment_inline_timeout_seconds",
            0.05,
        ),
        patch(
            "core_api.pipeline.steps.write.parallel_embed_enrich.settings.embed_on_hot_path",
            False,
        ),
        patch("core_api.services.memory_enrichment.enrich_memory", new=_slow_enrich),
        pytest.raises(HTTPException) as exc_info,
    ):
        await ParallelEmbedEnrich().execute(_ctx(enrichment=True))

    assert exc_info.value.status_code == 504
    assert "Memory write timed out" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Settings model validator
# ---------------------------------------------------------------------------


def test_validator_rejects_inline_timeout_at_or_above_request_timeout() -> None:
    """If the inline cap is >= request budget the outer middleware fires
    first and the 504 detail message is lost. Reject at config load.

    Calls the validator method directly on a model_construct-built
    instance to sidestep env-loading in the test runner — the field
    types and validator logic are what matter here."""
    s = Settings.model_construct(
        enrichment_inline_timeout_seconds=45.0,
        request_timeout_seconds=45.0,
    )
    with pytest.raises(ValueError, match="enrichment_inline_timeout_seconds"):
        Settings._validate_timeout_ordering(s)


def test_validator_accepts_default_ordering() -> None:
    """Sanity check the live singleton's defaults — 35s inline < 45s request."""
    assert _settings_singleton.enrichment_inline_timeout_seconds < _settings_singleton.request_timeout_seconds
    assert _settings_singleton.enrichment_inline_timeout_seconds == 35.0
    assert _settings_singleton.openai_request_timeout_seconds == 25.0
