"""ResolveSTMTarget — determine whether STM write goes to notes or bulletin."""

from __future__ import annotations

from fastapi import HTTPException

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult


class ResolveSTMTarget:
    @property
    def name(self) -> str:
        return "resolve_stm_target"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        data = ctx.data["input"]
        visibility = data.visibility or "scope_agent"

        if visibility == "scope_agent":
            ctx.data["stm_target"] = "notes"
        else:
            # scope_team and scope_org both go to fleet bulletin. The route has
            # already filled in the agent's home fleet, so no fleet here means
            # there is none. This used to fall back to fleet 'default', which
            # nothing reads back for the writer: search injects only the
            # caller's fleets, and GET /stm/bulletin refuses 'default' to a
            # trust-1 agent. The 201 promised an entry it could never see (M-26).
            if not data.fleet_id:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"A {visibility} short-term write goes to a fleet bulletin, and "
                        "neither the request nor the agent names a fleet. Pass fleet_id, "
                        "or write with visibility='scope_agent' to keep it in the "
                        "agent's own notes."
                    ),
                )
            ctx.data["stm_target"] = "bulletin"
            ctx.data["stm_fleet_id"] = data.fleet_id

        return None
