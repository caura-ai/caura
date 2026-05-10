"""Concurrent embedding + LLM enrichment via asyncio.gather.

When ``settings.embed_on_hot_path`` is False the embedding provider call
is skipped entirely — the row persists with embedding=NULL and
``ScheduleBackgroundTasks`` publishes ``Topics.Memory.EMBED_REQUESTED``
so ``core-worker`` backfills it asynchronously (CAURA-594 SaaS). When
True (OSS default), the embed runs inline; on inline failure the same
``ScheduleBackgroundTasks`` step schedules an in-process retry instead.

CAURA-595 mirror: ``settings.enrich_on_hot_path=False`` skips the inline
enrichment call too. The strong-write pipeline persists the row with
agent-provided values + schema defaults for the LLM-derived columns
(``memory_type`` / ``weight`` / ``status``); ``ScheduleBackgroundTasks``
publishes ``Topics.Memory.ENRICH_REQUESTED`` and ``core-worker``
PATCHes the enrichment fields back. Hint-based re-embed is DISABLED
(CAURA-222): writes used to embed
``compose_embedding_text(content, retrieval_hint)`` while queries embed
raw text, producing a write/query surface asymmetry that capped recall
across dedup, entity-lookup, and search ranking. Until a symmetric
reintroduction lands, both the hot-path here and the background
re-embed in ``_enrich_memory_background`` embed raw ``content``.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException

from common.embedding import get_embedding
from core_api.config import settings
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult

logger = logging.getLogger(__name__)


class ParallelEmbedEnrich:
    @property
    def name(self) -> str:
        return "parallel_embed_enrich"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        data = ctx.data["input"]
        tenant_config = ctx.tenant_config
        cached_embedding = ctx.data.get("cached_embedding")
        ch = ctx.data.get("content_hash")

        # A cached embedding is a hit on the idempotency/content-hash
        # cache (pure dict lookup, no provider call) so we reuse it even
        # when hot-path embed is off — nothing to offload.
        defer_embedding = not settings.embed_on_hot_path and cached_embedding is None

        embedding_task = None
        if cached_embedding is not None:
            logger.info("Reusing existing embedding for content_hash=%s", ch[:12])

            async def _return_cached():
                return cached_embedding

            embedding_task = _return_cached()
        elif not defer_embedding:
            embedding_task = get_embedding(data.content, tenant_config)

        # ``enrich_on_hot_path=False`` — defer the LLM call to
        # ``core-worker`` via ``Topics.Memory.ENRICH_REQUESTED`` published
        # by ``ScheduleBackgroundTasks``. The strong-write response
        # surface drops the LLM-derived fields (matches the user-accepted
        # Q4 design decision in CAURA-595).
        defer_enrichment = not settings.enrich_on_hot_path

        enrichment_task = None
        if (
            not defer_enrichment
            and tenant_config.enrichment_enabled
            and tenant_config.enrichment_provider != "none"
        ):
            from core_api.services.memory_enrichment import enrich_memory

            enrichment_task = enrich_memory(
                data.content, tenant_config, reference_datetime=data.reference_datetime
            )

        # Gather whichever subset of tasks exists; stays parallel when
        # both are present and avoids the gather overhead when only one
        # is present (or neither, in the unlikely all-cached-off case).
        pending = [t for t in (embedding_task, enrichment_task) if t is not None]
        results: list = []
        if pending:
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*pending),
                    timeout=settings.enrichment_inline_timeout_seconds,
                )
            except TimeoutError:
                # One message covers both paths — the single wait_for now
                # wraps embedding-only (flag=on, no enrichment) and
                # embedding+enrichment gather indiscriminately.
                raise HTTPException(status_code=504, detail="Memory write timed out (embedding/enrichment)")
        # Iterate rather than pop(0) — same effective semantics without
        # mutating ``results`` as a side-effect and without the subtle
        # O(n) shift that a list.pop(0) does.
        result_iter = iter(results)
        embedding = next(result_iter) if embedding_task is not None else None
        enrichment = next(result_iter) if enrichment_task is not None else None

        # Hint re-embed disabled (CAURA-222): writes embedded
        # `compose_embedding_text(content, retrieval_hint)` —
        # "[Retrieval hint]: ...\n\n<content>" — while queries embed raw
        # text. Identical content↔query produced cosine ~0.69 instead of
        # ~1.0, capping recall across dedup, entity_lookup, and search
        # ranking. The background hint re-embed in
        # `_enrich_memory_background` (memory_service._enrich_memory_background)
        # required the same fix; it now also embeds raw `content`. Both
        # sides — hot path and background — embed raw `content` to match
        # the search surface (raw query through `get_query_embedding`).

        ctx.data["embedding"] = embedding
        ctx.data["enrichment"] = enrichment
        return None
