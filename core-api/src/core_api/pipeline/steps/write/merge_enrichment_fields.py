"""MergeEnrichmentFields — apply LLM-inferred type/weight/title/tags/dates to memory fields."""

from __future__ import annotations

from datetime import datetime

from core_api.constants import DEFAULT_MEMORY_TYPE, DEFAULT_MEMORY_WEIGHT
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult


class MergeEnrichmentFields:
    @property
    def name(self) -> str:
        return "merge_enrichment_fields"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        data = ctx.data["input"]
        enrichment = ctx.data.get("enrichment")

        memory_type = data.memory_type
        weight = data.weight
        title = None
        metadata = data.metadata or {}
        ts_valid_start = data.ts_valid_start
        ts_valid_end = data.ts_valid_end

        if enrichment:
            # LLM fills gaps; agent-provided values always win
            if memory_type is None:
                memory_type = enrichment.memory_type
            if weight is None:
                weight = enrichment.weight
            title = enrichment.title or None
            if enrichment.summary:
                metadata["summary"] = enrichment.summary
            if enrichment.tags:
                metadata["tags"] = enrichment.tags
            if enrichment.llm_ms:
                metadata["llm_ms"] = enrichment.llm_ms
            # Temporal resolution: LLM-extracted dates fill gaps
            if ts_valid_start is None and enrichment.ts_valid_start:
                ts_valid_start = datetime.fromisoformat(enrichment.ts_valid_start.replace("Z", "+00:00"))
            if ts_valid_end is None and enrichment.ts_valid_end:
                ts_valid_end = datetime.fromisoformat(enrichment.ts_valid_end.replace("Z", "+00:00"))
            # PII detection
            if enrichment.contains_pii:
                metadata["contains_pii"] = True
                if enrichment.pii_types:
                    metadata["pii_types"] = enrichment.pii_types

        # Apply defaults if still unset (LLM disabled or failed)
        if memory_type is None:
            memory_type = DEFAULT_MEMORY_TYPE
        if weight is None:
            weight = DEFAULT_MEMORY_WEIGHT

        # Status: agent-provided wins, then LLM, then default "active"
        status = data.status
        if not status and enrichment:
            status = getattr(enrichment, "status", None)
        if not status:
            status = "active"

        # Write-mode metadata: track resolved mode and enrichment deferral
        resolved_write_mode = ctx.data.get("resolved_write_mode")
        if resolved_write_mode:
            metadata["write_mode"] = resolved_write_mode
        if resolved_write_mode == "fast" and enrichment is None:
            metadata["enrichment_pending"] = True

        ctx.data["memory_fields"] = {
            "memory_type": memory_type,
            "weight": weight,
            "title": title,
            "metadata": metadata,
            "ts_valid_start": ts_valid_start,
            "ts_valid_end": ts_valid_end,
            "status": status,
        }
        return None
