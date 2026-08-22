"""ResolveSearchProfile — adapt ``ctx.data`` to the shared knob resolver.

The resolution ladder itself (agent profile → tenant default → constant) lives in
``memory_service.resolve_search_params``, because the legacy search path needs the
identical answer.
"""

from __future__ import annotations

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult
from core_api.services.memory_service import resolve_search_params


class ResolveSearchProfile:
    @property
    def name(self) -> str:
        return "resolve_search_profile"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        ctx.data["search_params"] = resolve_search_params(
            ctx.data.get("search_profile"),
            query=ctx.data["query"],
            top_k=ctx.data["top_k"],
            tenant_config=ctx.tenant_config,
        )
        return None
