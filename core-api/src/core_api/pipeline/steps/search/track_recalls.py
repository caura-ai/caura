"""TrackRecalls — fire-and-forget recall tracking in a background task.

Uses its own DB session so the search response returns immediately without
waiting for the recall_count UPDATE + COMMIT round-trip.
"""

from __future__ import annotations

import logging
from uuid import UUID

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult
from core_api.services.hooks import get_hooks
from core_api.tasks import track_task

logger = logging.getLogger(__name__)


async def _track_recalls_background(memory_ids: list[UUID]) -> None:
    """Background task: update recall stats in an independent DB session."""
    from core_api.db.session import async_session

    try:
        _hooks = get_hooks()
        if _hooks.on_recall:
            async with async_session() as db:
                await _hooks.on_recall(db, memory_ids)
                await db.commit()
    except Exception:
        logger.warning("Background recall tracking failed", exc_info=True)


class TrackRecalls:
    @property
    def name(self) -> str:
        return "track_recalls"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        memory_ids = [row.Memory.id for row in ctx.data["filtered_rows"]]
        if memory_ids:
            track_task(_track_recalls_background(memory_ids))
        return None
