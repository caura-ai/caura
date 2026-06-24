"""Unit tests for the DiscoverCrossLinks pipeline step.

DB-free as of Fix 2 Ph6: the step folds candidate-find + pgvector LATERAL +
text-verify + bulk ON-CONFLICT insert into one atomic core-storage-api call
(``sc.discover_cross_links``). These mock the storage client and assert the
step forwards its tuning, passes ``target_memory_ids`` through for targeted
mode, and maps ``links_created`` into ``ctx.data`` + the StepResult.

The SQL-shape regression anchors (CAURA-686 single multi-VALUES RETURNING, the
``::uuid[]`` CAURA-675 guard, targeted-vs-batch mode) now live storage-side in
``tests/test_ph6_entity_linking_storage.py`` against the real DB.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome
from core_api.pipeline.steps.entity_linking.discover_cross_links import (
    DiscoverCrossLinks,
)

TENANT = "test-tenant"


def _make_ctx(**extra_data):
    # ``db=None`` — the step no longer touches a DB session.
    return PipelineContext(data={"tenant_id": TENANT, **extra_data})


def _patch_sc(resp: dict) -> tuple:
    sc = MagicMock()
    sc.discover_cross_links = AsyncMock(return_value=resp)
    return sc, patch(
        "core_api.pipeline.steps.entity_linking.discover_cross_links.get_storage_client",
        return_value=sc,
    )


@pytest.mark.asyncio
async def test_discover_creates_links_sets_ctx_and_result():
    sc, p = _patch_sc({"links_created": 3})
    with p:
        ctx = _make_ctx()
        result = await DiscoverCrossLinks().execute(ctx)

    assert result.outcome == StepOutcome.SUCCESS
    assert result.detail["links_created"] == 3
    assert ctx.data["links_created"] == 3


@pytest.mark.asyncio
async def test_discover_zero_links_is_success_with_zero():
    sc, p = _patch_sc({"links_created": 0})
    with p:
        ctx = _make_ctx()
        result = await DiscoverCrossLinks().execute(ctx)

    assert result.outcome == StepOutcome.SUCCESS
    assert ctx.data["links_created"] == 0


@pytest.mark.asyncio
async def test_discover_skipped_when_storage_signals_skip():
    # storage flags the no-candidates case with ``skipped`` so the step
    # reproduces the source's StepOutcome.SKIPPED (vs SUCCESS for zero links).
    sc, p = _patch_sc({"skipped": True, "links_created": 0})
    with p:
        ctx = _make_ctx()
        result = await DiscoverCrossLinks().execute(ctx)

    assert result.outcome == StepOutcome.SKIPPED


@pytest.mark.asyncio
async def test_discover_forwards_targeted_memory_ids():
    mem_id = uuid.uuid4()
    sc, p = _patch_sc({"links_created": 1})
    with p:
        ctx = _make_ctx(target_memory_ids=[mem_id], cross_link_text_verify=False)
        await DiscoverCrossLinks().execute(ctx)

    kwargs = sc.discover_cross_links.await_args.kwargs
    assert kwargs["target_memory_ids"] == [mem_id]
    assert kwargs["text_verify"] is False
    assert kwargs["tenant_id"] == TENANT


@pytest.mark.asyncio
async def test_discover_batch_mode_passes_no_target_ids():
    sc, p = _patch_sc({"links_created": 0})
    with p:
        ctx = _make_ctx()
        await DiscoverCrossLinks().execute(ctx)

    kwargs = sc.discover_cross_links.await_args.kwargs
    assert kwargs["target_memory_ids"] is None
