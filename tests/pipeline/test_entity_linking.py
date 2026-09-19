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


@pytest.mark.asyncio
async def test_a_slow_storage_call_fails_with_a_named_budget_not_a_bare_timeout():
    """The step's own budget must win the race against httpx and Cloud Run.

    ``POST /entities/discover-cross-links`` had no application-level cap, so
    its only limit was the writer's 120s Cloud Run request timeout — EQUAL to
    the storage client's 120s httpx read. The storage client's own docstring
    calls that shape out: "Equal values would 50/50 race." Whichever fired,
    the caller learned nothing. The 2026-09-18 staging dead-letter recorded
    ``failures=["ReadTimeout('')"]`` — an empty string where the reason goes —
    ten times over.

    Asserting the DETAIL, not just the outcome: a FAILED step that still said
    nothing would be the same operational dead end.
    """
    import asyncio as _asyncio

    from core_api.config import settings

    async def _never_returns(**_kwargs):
        await _asyncio.sleep(3600)

    sc = MagicMock()
    sc.discover_cross_links = _never_returns

    with (
        patch(
            "core_api.pipeline.steps.entity_linking.discover_cross_links.get_storage_client",
            return_value=sc,
        ),
        patch.object(settings, "cross_link_request_timeout_seconds", 0.05),
    ):
        ctx = _make_ctx()
        # Outer guard so the ABSENCE of the fix fails this test instead of
        # hanging the suite: with no budget inside the step there is nothing
        # to cancel the stalled call, and the assertions below are never
        # reached. 5s is ~100x the patched budget, so it cannot mask a slow
        # machine.
        result = await _asyncio.wait_for(DiscoverCrossLinks().execute(ctx), timeout=5)

    assert result.outcome == StepOutcome.FAILED
    assert "budget" in result.detail["error"], (
        f"the failure does not name what was exceeded: {result.detail}"
    )
    # Cancelling the wait cannot roll back a transaction that may already have
    # committed, so the message must not tell on-call the batch did not land.
    assert "no links landed" not in result.detail["error"], (
        "the message asserts server-side state the client cannot observe: "
        f"{result.detail['error']}"
    )
    assert result.detail["tenant_id"] == TENANT, (
        "the failure must name the tenant — one org out of hundreds is the "
        "whole diagnostic question"
    )


def test_the_budget_sits_below_both_120s_ceilings():
    """The ordering is the point, so it is pinned rather than left to a comment.

    Above either ceiling this cancellation stops winning and the opaque
    ``ReadTimeout('')`` comes back. Named constants rather than the literal
    120.0, so that raising one of the two ceilings moves this assertion with
    it instead of leaving it asserting a number nothing uses any more.
    """
    from core_api.config import PLATFORM_REQUEST_CEILING_SECONDS, settings
    from core_api.constants import STORAGE_READ_TIMEOUT_SECONDS

    assert settings.cross_link_request_timeout_seconds < STORAGE_READ_TIMEOUT_SECONDS, (
        "the budget must beat the storage client's httpx read timeout, or "
        "httpx fires first and the caller gets ReadTimeout('') instead"
    )
    assert (
        settings.cross_link_request_timeout_seconds < PLATFORM_REQUEST_CEILING_SECONDS
    ), "the budget must also beat the writer's Cloud Run request ceiling"


@pytest.mark.parametrize("override", ["120.0", "150.0"])
def test_startup_rejects_a_budget_that_cannot_win_the_race(monkeypatch, override):
    """The ordering above is ENFORCED, not merely asserted about the default.

    The default is safe; the env override is the exposed surface, and it is
    the one an operator reaches for while debugging exactly the timeout this
    budget exists to make legible ("it timed out, so raise the timeout").
    At or above the binding ceiling that change is worse than a no-op: the
    step stops cancelling first and the failure reverts to an opaque
    ``ReadTimeout('')``, with nothing at startup to say so.

    ``120.0`` is the case a ``>`` comparison would wave through, and it is
    not hypothetical -- it is the exact configuration that failed staging on
    2026-09-18, where the client read timeout and the writer's Cloud Run
    ceiling were both 120s and raced.
    """
    import core_api.config as config_mod

    monkeypatch.setenv("CROSS_LINK_REQUEST_TIMEOUT_SECONDS", override)
    with pytest.raises(ValueError, match="cross_link_request_timeout_seconds"):
        config_mod.Settings()
