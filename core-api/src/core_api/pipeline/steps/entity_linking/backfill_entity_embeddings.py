"""BackfillEntityEmbeddings — generate name_embedding for entities that lack one.

Partially DB-free as of Fix 2 Ph6: the NULL-embedding read and the embedding
write-back are routed through core-storage-api (``POST
/entities/list-null-embeddings`` and ``POST /entities/set-embeddings``), but the
LLM ``get_embedding`` loop in the middle MUST stay in core-api — storage has no
embedding/provider chain. So this step is read → core-api LLM loop → write.
"""

from __future__ import annotations

import logging

from common.embedding import get_embedding
from core_api.clients.storage_client import get_storage_client
from core_api.constants import ENTITY_EMBEDDING_BACKFILL_BATCH_SIZE
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult

logger = logging.getLogger(__name__)


class BackfillEntityEmbeddings:
    @property
    def name(self) -> str:
        return "backfill_entity_embeddings"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        """Embed entities whose name_embedding is NULL."""
        tenant_id: str = ctx.data["tenant_id"]
        fleet_id: str | None = ctx.data.get("fleet_id")
        batch_size: int = ctx.data.get(
            "entity_embedding_backfill_batch_size",
            ENTITY_EMBEDDING_BACKFILL_BATCH_SIZE,
        )

        sc = get_storage_client()
        rows = await sc.list_null_embedding_entities(
            tenant_id=tenant_id,
            fleet_id=fleet_id,
            batch_size=batch_size,
        )

        if not rows:
            return StepResult(outcome=StepOutcome.SKIPPED)

        updates: list[dict] = []
        for row in rows:
            eid = row["id"]
            canonical_name = row["canonical_name"]
            try:
                embedding = await get_embedding(canonical_name, ctx.tenant_config)
            except Exception:
                logger.warning("Failed to embed entity %s (%s)", eid, canonical_name, exc_info=True)
                continue

            if embedding is not None:
                updates.append({"id": str(eid), "embedding": embedding})

        backfill_count = 0
        if updates:
            backfill_count = await sc.set_entity_embeddings(tenant_id=tenant_id, updates=updates)

        ctx.data["backfill_count"] = backfill_count

        logger.info(
            "Backfilled %d/%d entity embeddings for tenant %s",
            backfill_count,
            len(rows),
            tenant_id,
        )
        return StepResult(
            outcome=StepOutcome.SUCCESS,
            detail={"backfill_count": backfill_count},
        )
