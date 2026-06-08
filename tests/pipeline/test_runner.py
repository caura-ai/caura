"""Unit tests for the pipeline runner — timing, skip, fail, short-circuit."""

import pytest
from unittest.mock import AsyncMock

from fastapi import HTTPException

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.runner import Pipeline
from core_api.pipeline.step import StepOutcome, StepResult


# ---------------------------------------------------------------------------
# Helpers: minimal step implementations
# ---------------------------------------------------------------------------


class SuccessStep:
    @property
    def name(self) -> str:
        return "success_step"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        ctx.data["ran_success"] = True
        return None


class SkipStep:
    @property
    def name(self) -> str:
        return "skip_step"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        return StepResult(outcome=StepOutcome.SKIPPED)


class FailStep:
    @property
    def name(self) -> str:
        return "fail_step"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        raise RuntimeError("something broke")


class HttpErrorStep:
    @property
    def name(self) -> str:
        return "http_error_step"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        raise HTTPException(status_code=409, detail="Duplicate")


class TrackingStep:
    """Step that records execution order in ctx.data['order']."""

    def __init__(self, label: str):
        self._label = label

    @property
    def name(self) -> str:
        return self._label

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        ctx.data.setdefault("order", []).append(self._label)
        return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_pipeline():
    """All steps succeed — result reflects step count and no failures."""
    db = AsyncMock()
    ctx = PipelineContext(db=db)
    pipeline = Pipeline("test", [SuccessStep(), SuccessStep()])

    result = await pipeline.run(ctx)

    assert not result.failed
    assert result.step_count == 2
    assert result.skipped_count == 0
    assert result.total_ms > 0
    assert ctx.data["ran_success"] is True


@pytest.mark.asyncio
async def test_skip_counted():
    """Skipped steps are counted in skipped_count."""
    db = AsyncMock()
    ctx = PipelineContext(db=db)
    pipeline = Pipeline("test", [SuccessStep(), SkipStep()])

    result = await pipeline.run(ctx)

    assert not result.failed
    assert result.step_count == 2
    assert result.skipped_count == 1


@pytest.mark.asyncio
async def test_fail_short_circuits():
    """Non-HTTP exception fails pipeline and stops execution."""
    db = AsyncMock()
    ctx = PipelineContext(db=db)
    after = TrackingStep("after_fail")
    pipeline = Pipeline("test", [FailStep(), after])

    result = await pipeline.run(ctx)

    assert result.failed
    assert "order" not in ctx.data  # after_fail never ran
    assert len(result.steps) == 1
    assert result.steps[0].outcome == StepOutcome.FAILED
    assert isinstance(result.steps[0].error, RuntimeError)


@pytest.mark.asyncio
async def test_http_exception_propagates():
    """HTTPException raised by a step propagates directly (not wrapped)."""
    db = AsyncMock()
    ctx = PipelineContext(db=db)
    pipeline = Pipeline("test", [HttpErrorStep()])

    with pytest.raises(HTTPException) as exc_info:
        await pipeline.run(ctx)

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_step_execution_order():
    """Steps execute in declared order."""
    db = AsyncMock()
    ctx = PipelineContext(db=db)
    pipeline = Pipeline(
        "test",
        [
            TrackingStep("a"),
            TrackingStep("b"),
            TrackingStep("c"),
        ],
    )

    await pipeline.run(ctx)

    assert ctx.data["order"] == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_per_step_timing():
    """Each step result is recorded in pipeline result."""
    db = AsyncMock()
    ctx = PipelineContext(db=db)
    pipeline = Pipeline("test", [SuccessStep()])

    result = await pipeline.run(ctx)

    assert len(result.steps) == 1
    assert result.steps[0].outcome == StepOutcome.SUCCESS


@pytest.mark.asyncio
async def test_empty_pipeline():
    """Pipeline with no steps completes successfully."""
    db = AsyncMock()
    ctx = PipelineContext(db=db)
    pipeline = Pipeline("empty", [])

    result = await pipeline.run(ctx)

    assert not result.failed
    assert result.step_count == 0
    assert result.total_ms >= 0
