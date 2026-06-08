"""ResolveSTMTarget — determine whether STM write goes to notes or bulletin."""

from __future__ import annotations

import logging

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult

logger = logging.getLogger(__name__)


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
            # scope_team and scope_org both go to fleet bulletin
            ctx.data["stm_target"] = "bulletin"
            ctx.data["stm_fleet_id"] = data.fleet_id
            if not data.fleet_id:
                logger.debug("fleet_id is None for bulletin write; falling back to 'default' fleet")

        return None
