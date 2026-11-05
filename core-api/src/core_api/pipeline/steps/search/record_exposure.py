"""RecordExposure — which agent this search handed each memory to (migration 072).

Correction propagation, D1: when a person corrects a memory, the agents that
read it are the ones to tell. ``core_api.services.memory_exposure`` has the
rules (chunks count for their parent, person reads are skipped). Always on,
fire-and-forget like ``LogRecallEvent``: nothing here adds request latency, and
a failure never breaks a recall.

A diagnostic search still returns its rows to the caller, so it counts.
``search_explain`` does not run this step: an explanation is not a recall.
"""

from __future__ import annotations

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult
from core_api.services.memory_exposure import schedule_exposure


class RecordExposure:
    @property
    def name(self) -> str:
        return "record_exposure"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        schedule_exposure(
            ctx.data.get("results") or [],
            agent_id=ctx.data.get("caller_agent_id"),
            tenant_id=ctx.data.get("tenant_id"),
        )
        return None
