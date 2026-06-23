"""ResolveEntities — merge duplicate entities via embedding-based resolution.

DB-free as of Fix 2 Ph6: the entire merge (pgvector pair-find, union-find
clustering, per-dupe SAVEPOINT re-pointing) runs in ONE atomic core-storage-api
transaction behind ``POST /entities/resolve``. This step only reads tuning from
``ctx.data`` + ``core_api.constants``, calls the storage client, and maps the
response into the same ``ctx.data`` outputs + ``StepResult`` it produced before.
"""

from __future__ import annotations

import logging

from core_api.clients.storage_client import get_storage_client
from core_api.constants import (
    ENTITY_RESOLUTION_BATCH_SIZE,
    ENTITY_RESOLUTION_CANDIDATE_LIMIT,
    ENTITY_RESOLUTION_THRESHOLD,
)
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult

logger = logging.getLogger(__name__)


class ResolveEntities:
    """Merge duplicate entities whose name embeddings exceed the similarity threshold."""

    @property
    def name(self) -> str:
        return "resolve_entities"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        tenant_id: str = ctx.data["tenant_id"]
        fleet_id: str | None = ctx.data.get("fleet_id")
        batch_size: int = ctx.data.get("entity_resolution_batch_size", ENTITY_RESOLUTION_BATCH_SIZE)
        threshold: float = ctx.data.get("entity_resolution_threshold", ENTITY_RESOLUTION_THRESHOLD)

        resp = await get_storage_client().resolve_entities(
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            batch_size=batch_size,
            threshold=threshold,
            candidate_limit=ENTITY_RESOLUTION_CANDIDATE_LIMIT,
        )

        # No similar pairs found → nothing to merge (mirrors the source's
        # early SKIPPED return when the pair-find query is empty). The storage
        # method flags exactly this case with ``skipped`` so a pairs-found-but-
        # zero-merges run still returns SUCCESS(0) like the source.
        if resp.get("skipped"):
            return StepResult(StepOutcome.SKIPPED)

        # Storage reports "all clusters failed to merge" as an ``error`` key
        # (it cannot return a StepResult across HTTP). Map it back to FAILED.
        if "error" in resp:
            return StepResult(
                StepOutcome.FAILED,
                detail={
                    "error": resp["error"],
                    "cluster_errors": resp.get("cluster_errors", 0),
                },
            )

        merged_entity_ids = resp.get("merged_entity_ids", [])
        ctx.data["merge_count"] = resp.get("merge_count", 0)
        ctx.data["merged_entity_ids"] = merged_entity_ids

        return StepResult(
            StepOutcome.SUCCESS,
            detail={
                "merge_count": resp.get("merge_count", 0),
                "clusters": resp.get("clusters", 0),
                "cluster_errors": resp.get("cluster_errors", 0),
                "merged_entity_ids": merged_entity_ids,
            },
        )
