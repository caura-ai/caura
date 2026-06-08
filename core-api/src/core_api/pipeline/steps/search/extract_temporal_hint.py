"""ExtractTemporalHint — auto-detect time scope from query.

Sets two context keys:
- ``temporal_window``: soft freshness boost (timedelta or None)
- ``date_range_filter``: hard WHERE filter (dict or None)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult
from core_api.services.memory_service import (
    _extract_temporal_date_range,
    _extract_temporal_hint,
)

logger = logging.getLogger(__name__)


class ExtractTemporalHint:
    @property
    def name(self) -> str:
        return "extract_temporal_hint"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        query = ctx.data["query"]
        ctx.data["temporal_window"] = _extract_temporal_hint(query)

        reference_dt = ctx.data.get("valid_at") or datetime.now(UTC)
        ctx.data["date_range_filter"] = _extract_temporal_date_range(query, reference_dt)
        logger.info(
            "temporal_hint: query=%r ref=%s window=%s date_range=%s",
            query[:80],
            reference_dt,
            ctx.data["temporal_window"],
            ctx.data["date_range_filter"],
        )
        return None
