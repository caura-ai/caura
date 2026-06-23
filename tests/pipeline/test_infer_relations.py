"""Unit tests for the InferRelations pipeline step.

DB-free as of Fix 2 Ph6: the step folds the co-occurrence scan + existing-
relation lookup + reinforce-vs-create split + batched UPDATE/INSERT into one
atomic core-storage-api call (``sc.infer_relations``). These mock the storage
client and assert the step forwards its tuning (incl. the
``reinforce_delta``/``max_relation_weight`` constants the prod clamp relies on)
and maps the created/reinforced counts into ``ctx.data`` + the StepResult.

The reinforce-UPDATE regression anchor (prod 2026-06-13: ``SET weight =
:new_weight`` NOT ``LEAST(:a,:b)`` over untyped binds → asyncpg
DatatypeMismatchError; the Python clamp) now lives storage-side in
``tests/test_ph6_entity_linking_storage.py`` against the real DB.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.constants import MAX_RELATION_WEIGHT, RELATION_REINFORCE_DELTA
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome
from core_api.pipeline.steps.entity_linking.infer_relations import InferRelations

TENANT = "test-tenant"


def _ctx(**extra):
    return PipelineContext(db=None, data={"tenant_id": TENANT, **extra})


def _patch_sc(resp: dict) -> tuple:
    sc = MagicMock()
    sc.infer_relations = AsyncMock(return_value=resp)
    return sc, patch(
        "core_api.pipeline.steps.entity_linking.infer_relations.get_storage_client",
        return_value=sc,
    )


@pytest.mark.asyncio
async def test_infer_sets_counts_on_ctx_and_result():
    sc, p = _patch_sc({"relations_created": 2, "relations_reinforced": 5})
    with p:
        ctx = _ctx()
        result = await InferRelations().execute(ctx)

    assert result.outcome == StepOutcome.SUCCESS
    assert ctx.data["relations_created"] == 2
    assert ctx.data["relations_reinforced"] == 5
    assert result.detail == {"relations_created": 2, "relations_reinforced": 5}


@pytest.mark.asyncio
async def test_infer_forwards_clamp_constants():
    """The Python-side clamp the prod fix relies on lives storage-side now;
    the step must forward the delta + max-weight constants so storage can
    apply it (and bind the already-clamped value, never LEAST(:a,:b))."""
    sc, p = _patch_sc({"relations_created": 0, "relations_reinforced": 0})
    with p:
        await InferRelations().execute(_ctx())

    kwargs = sc.infer_relations.await_args.kwargs
    assert kwargs["reinforce_delta"] == pytest.approx(RELATION_REINFORCE_DELTA)
    assert kwargs["max_relation_weight"] == pytest.approx(MAX_RELATION_WEIGHT)
    assert kwargs["tenant_id"] == TENANT


@pytest.mark.asyncio
async def test_infer_zero_counts_still_success():
    sc, p = _patch_sc({"relations_created": 0, "relations_reinforced": 0})
    with p:
        ctx = _ctx()
        result = await InferRelations().execute(ctx)

    assert result.outcome == StepOutcome.SUCCESS
    assert ctx.data["relations_created"] == 0
    assert ctx.data["relations_reinforced"] == 0


@pytest.mark.asyncio
async def test_infer_skipped_when_storage_signals_skip():
    # storage flags the no-co-occurrence case with ``skipped`` so the step
    # reproduces the source's StepOutcome.SKIPPED (vs SUCCESS for zero counts).
    sc, p = _patch_sc({"skipped": True, "relations_created": 0, "relations_reinforced": 0})
    with p:
        result = await InferRelations().execute(_ctx())

    assert result.outcome == StepOutcome.SKIPPED
