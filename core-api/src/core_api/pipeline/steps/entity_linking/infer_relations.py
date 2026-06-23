"""InferRelations — create co-occurrence-based relations between entities.

DB-free as of Fix 2 Ph6: the co-occurrence scan, existing-relation lookup, the
reinforce-vs-create split, and the batched UPDATE/INSERT all run in ONE atomic
core-storage-api transaction behind ``POST /entities/infer-relations``. This
step reads tuning from ``ctx.data`` + ``core_api.constants`` and calls the
storage client.
"""

from __future__ import annotations

import logging

from core_api.clients.storage_client import get_storage_client
from core_api.constants import (
    MAX_RELATION_WEIGHT,
    MIN_COOCCURRENCE_FOR_RELATION,
    RELATION_INFERENCE_BATCH_SIZE,
    RELATION_REINFORCE_DELTA,
)
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult

logger = logging.getLogger(__name__)


class InferRelations:
    """Infer 'related_to' relations from entity co-occurrence in shared memories."""

    @property
    def name(self) -> str:
        return "infer_relations"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        """Scan co-occurring entity pairs and create or reinforce relations."""
        tenant_id: str = ctx.data["tenant_id"]
        fleet_id: str | None = ctx.data.get("fleet_id")
        batch_size: int = ctx.data.get("relation_inference_batch_size", RELATION_INFERENCE_BATCH_SIZE)
        min_cooccurrence: int = ctx.data.get("min_cooccurrence", MIN_COOCCURRENCE_FOR_RELATION)

        resp = await get_storage_client().infer_relations(
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            batch_size=batch_size,
            min_cooccurrence=min_cooccurrence,
            reinforce_delta=RELATION_REINFORCE_DELTA,
            max_relation_weight=MAX_RELATION_WEIGHT,
        )

        # No co-occurring pairs → nothing to infer (mirrors the source's SKIPPED).
        if resp.get("skipped"):
            return StepResult(outcome=StepOutcome.SKIPPED)

        relations_created = resp.get("relations_created", 0)
        relations_reinforced = resp.get("relations_reinforced", 0)
        ctx.data["relations_created"] = relations_created
        ctx.data["relations_reinforced"] = relations_reinforced

        logger.info(
            "Inferred relations for tenant %s: created=%d reinforced=%d",
            tenant_id,
            relations_created,
            relations_reinforced,
        )
        return StepResult(
            outcome=StepOutcome.SUCCESS,
            detail={
                "relations_created": relations_created,
                "relations_reinforced": relations_reinforced,
            },
        )
