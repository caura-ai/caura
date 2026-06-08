"""CheckContentLength — reject content below minimum character threshold."""

from __future__ import annotations

from fastapi import HTTPException

from core_api.constants import CRYSTALLIZER_SHORT_CONTENT_CHARS
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult


class CheckContentLength:
    @property
    def name(self) -> str:
        return "check_content_length"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        data = ctx.data["input"]
        if len(data.content.strip()) < CRYSTALLIZER_SHORT_CONTENT_CHARS:
            raise HTTPException(
                status_code=422,
                detail=f"Memory content too short (minimum {CRYSTALLIZER_SHORT_CONTENT_CHARS} characters).",
            )
        return None
