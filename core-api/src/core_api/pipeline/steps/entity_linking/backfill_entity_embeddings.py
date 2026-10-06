"""BackfillEntityEmbeddings — generate name_embedding for entities that lack one.

Partially DB-free as of Fix 2 Ph6: the NULL-embedding read and the embedding
write-back are routed through core-storage-api (``POST
/entities/list-null-embeddings`` and ``POST /entities/set-embeddings``), but the
LLM embedding in the middle MUST stay in core-api — storage has no
embedding/provider chain. So this step is read → core-api embed → write.

L-174. It used to await one ``get_embedding`` per row (a provider request and a
background-gate slot each) over an unordered scan, so names that fail every
night came back first every night and could stall the rows behind them. It now
embeds each page with one ``get_embeddings_batch`` call and pages past what
failed, through the scan's ``after_id`` cursor.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from common.embedding import get_embedding, get_embeddings_batch
from core_api.clients.storage_client import get_storage_client
from core_api.constants import ENTITY_EMBEDDING_BACKFILL_BATCH_SIZE
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult

logger = logging.getLogger(__name__)

# Pages read per run. Bounds the cost of a run that meets a long run of names
# that will not embed, while still reaching the rows behind a few pages of them.
_MAX_PAGES = 4
# Per-name calls in flight when a page's batch call fails and the page falls
# back to one call per name.
_FALLBACK_CONCURRENCY = 8


async def _embed_page(rows: list[dict], tenant_config: Any) -> list[dict]:
    """``{id, embedding}`` for each row that embedded.

    One batch call for the page. If it fails, every name is tried on its own, so
    one name the provider rejects costs only itself.
    """
    names = [row["canonical_name"] for row in rows]
    try:
        embeddings = await get_embeddings_batch(names, tenant_config, background=True)
    except Exception:
        logger.warning(
            "Batch embed failed for %d entities; embedding one at a time", len(rows), exc_info=True
        )
        sem = asyncio.Semaphore(_FALLBACK_CONCURRENCY)

        async def one(row: dict) -> list[float] | None:
            async with sem:
                try:
                    return await get_embedding(row["canonical_name"], tenant_config, background=True)
                except Exception:
                    logger.warning(
                        "Failed to embed entity %s (%s)", row["id"], row["canonical_name"], exc_info=True
                    )
                    return None

        embeddings = await asyncio.gather(*(one(row) for row in rows))
    return [
        {"id": str(row["id"]), "embedding": embedding}
        for row, embedding in zip(rows, embeddings)
        if embedding is not None
    ]


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
        updates: list[dict] = []
        scanned = 0
        after_id: str | None = None
        for _ in range(_MAX_PAGES):
            rows = await sc.list_null_embedding_entities(
                tenant_id=tenant_id,
                fleet_id=fleet_id,
                batch_size=batch_size,
                after_id=after_id,
            )
            if not rows:
                break
            scanned += len(rows)
            updates.extend(await _embed_page(rows, ctx.tenant_config))
            # A short page is the end of the scan; a full batch is the run's work.
            if len(rows) < batch_size or len(updates) >= batch_size:
                break
            after_id = str(rows[-1]["id"])

        if not scanned:
            return StepResult(outcome=StepOutcome.SKIPPED)

        backfill_count = 0
        if updates:
            backfill_count = await sc.set_entity_embeddings(tenant_id=tenant_id, updates=updates)

        ctx.data["backfill_count"] = backfill_count

        logger.info(
            "Backfilled %d/%d entity embeddings for tenant %s",
            backfill_count,
            scanned,
            tenant_id,
        )
        return StepResult(
            outcome=StepOutcome.SUCCESS,
            detail={"backfill_count": backfill_count},
        )
