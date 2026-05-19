"""Concurrent embedding + LLM enrichment via asyncio.gather.

Deferral is now ``write_mode``-aware (restores the original CAURA-229
contract that the CAURA-524 step-consolidation + CAURA-595 PR-C global-
flag pattern inadvertently flattened):

* ``write_mode == "strong"`` → embed and enrich **always inline**,
  regardless of ``settings.deployment_mode``. Strong callers
  explicitly opted into "thorough write before commit"; the
  per-deploy mode targets fast-mode latency, not strong. Running
  inline here is what gives strong its meaningful guarantees —
  ``CheckSemanticDuplicate`` runs against a real embedding (so the
  409-on-near-duplicate contract holds), and the agent reads its own
  enriched ``title`` / ``memory_type`` / ``weight`` / ``status`` /
  ``ts_valid_*`` back in the response.
* ``write_mode == "fast"`` → LLM enrichment **always deferred**
  (matches CAURA-229's "fast = no LLM on the request path"
  intent). Embedding follows ``settings.inline_embedding`` so OSS
  local stays inline ~200ms while SaaS prod defers to ``core-worker``
  for the sub-2s p99 visibility SLA.
* Any other ``write_mode`` value (and the ``None`` case the
  enrichment-only sub-pipeline hits during extract-only / auto-chunk)
  reads both helpers — preserves today's behaviour for those
  branches.

Behind both deferred paths: ``ScheduleBackgroundTasks`` publishes
``Topics.Memory.EMBED_REQUESTED`` / ``Topics.Memory.ENRICH_REQUESTED``
so ``core-worker`` runs the provider call and PATCHes the row back.
Hint-based re-embed is DISABLED (CAURA-222): writes used to embed
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
        resolved_write_mode = ctx.data.get("resolved_write_mode")

        # write_mode-aware deferral. See the module docstring for the
        # contracts each mode enforces. A cached embedding is a hit on
        # the idempotency/content-hash cache (pure dict lookup, no
        # provider call) so we reuse it even when hot-path embed is off
        # — nothing to offload.
        # F3 Phase 2 batch 1: read the new ``deployment_mode``-derived
        # helpers instead of the legacy ``embed_on_hot_path`` /
        # ``enrich_on_hot_path`` flags. Polarity matches:
        # ``settings.inline_embedding == settings.embed_on_hot_path``
        # under the canonical (T,T) / (F,F) deployments — the only
        # configurations any real environment uses per the Phase 0
        # audit. The legacy flags continue to back the helpers via the
        # Phase 1 derivation validator until F3 Phase 3 deletes them.
        if resolved_write_mode == "strong":
            defer_embedding = False
            defer_enrichment = False
        elif resolved_write_mode == "fast":
            defer_embedding = (not settings.inline_embedding) and cached_embedding is None
            defer_enrichment = True
        else:
            defer_embedding = (not settings.inline_embedding) and cached_embedding is None
            defer_enrichment = not settings.inline_enrichment

        embedding_task = None
        if cached_embedding is not None:
            logger.info("Reusing existing embedding for content_hash=%s", ch[:12])

            async def _return_cached():
                return cached_embedding

            embedding_task = _return_cached()
        elif not defer_embedding:
            embedding_task = get_embedding(data.content, tenant_config)

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
