"""Pipeline runner — executes steps sequentially with per-step timing."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import Step, StepOutcome, StepResult

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    pipeline_name: str
    steps: list[StepResult] = field(default_factory=list)
    total_ms: float = 0.0
    step_count: int = 0
    skipped_count: int = 0
    failed: bool = False


class Pipeline:
    """Runs a sequence of steps with timing and structured logging."""

    def __init__(self, name: str, steps: list[Any]) -> None:
        self._name = name
        self._steps: list[Step] = steps

    async def run(self, ctx: PipelineContext) -> PipelineResult:
        result = PipelineResult(pipeline_name=self._name)
        t_pipeline = time.perf_counter()

        for step in self._steps:
            t_step = time.perf_counter()
            try:
                step_result = await step.execute(ctx)
                if step_result is None:
                    step_result = StepResult(outcome=StepOutcome.SUCCESS)
            except HTTPException:
                # Preserve existing HTTP error behavior — propagate as-is
                raise
            except Exception as exc:
                step_ms = (time.perf_counter() - t_step) * 1000
                # Errors always log, so no isEnabledFor guard here.
                logger.error(
                    "%s.%s: FAILED in %.1fms — %s",
                    self._name,
                    step.name,
                    step_ms,
                    exc,
                    exc_info=True,
                    extra={
                        "pipeline": self._name,
                        "step": step.name,
                        "step_ms": step_ms,
                        "step_outcome": "failed",
                    },
                )
                step_result = StepResult(
                    outcome=StepOutcome.FAILED,
                    error=exc,
                )
                result.steps.append(step_result)
                result.failed = True
                break

            step_ms = (time.perf_counter() - t_step) * 1000
            # Skip the extra-dict build when INFO is filtered out — at 10k QPS
            # x N steps, every saved allocation matters if the caller has
            # raised the log level (e.g. under sustained load).
            if logger.isEnabledFor(logging.INFO):
                logger.info(
                    "%s.%s: %.1fms",
                    self._name,
                    step.name,
                    step_ms,
                    extra={
                        "pipeline": self._name,
                        "step": step.name,
                        "step_ms": step_ms,
                        "step_outcome": step_result.outcome.value,
                    },
                )
            result.steps.append(step_result)
            result.step_count += 1
            if step_result.outcome == StepOutcome.SKIPPED:
                result.skipped_count += 1
            elif step_result.outcome == StepOutcome.FAILED:
                # OSS 08/14 M-06 — a step that RETURNS ``FAILED`` used to land
                # here and be counted as an ordinary step: ``result.failed``
                # stayed False and the pipeline reported success. Only the
                # ``except`` arm above set it, so the flag meant "a step raised"
                # rather than "a step failed", and the one step that reports its
                # failure by returning (``ResolveEntities``, mapping storage's
                # "all clusters failed to merge" back from an ``error`` key it
                # cannot raise across HTTP) was invisible to every caller.
                #
                # Recorded, but NOT a break, and the asymmetry with the raise
                # path is the contract rather than an oversight: a step that
                # returns ``FAILED`` has caught its own error and chosen to
                # report it, so the steps after it still run — for the nightly
                # entity-linking pipeline that keeps cross-link discovery and
                # relation inference working when only the merge stage failed.
                # Raising still aborts. "I failed" and "stop the pipeline" are
                # different statements, and a step now has a way to say each.
                result.failed = True

        result.total_ms = (time.perf_counter() - t_pipeline) * 1000
        if logger.isEnabledFor(logging.INFO):
            logger.info(
                "%s: total=%.1fms steps=%d skipped=%d",
                self._name,
                result.total_ms,
                result.step_count,
                result.skipped_count,
                extra={
                    "pipeline": self._name,
                    "pipeline_total_ms": result.total_ms,
                    "step_count": result.step_count,
                    "skipped_count": result.skipped_count,
                },
            )
        return result
