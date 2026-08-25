"""MergeEnrichmentFields — apply LLM-inferred type/weight/title/tags/dates to memory fields."""

from __future__ import annotations

from datetime import datetime

from common.enrichment.constants import CLASSIFIER_DEPRECATED_MEMORY_TYPES
from core_api.constants import DEFAULT_MEMORY_TYPE, DEFAULT_MEMORY_WEIGHT
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult
from core_api.services.system_metadata import set_system_value


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
        # C25 — the platform/caller metadata boundary. Caller input was
        # sanitized at the ``create_memory`` chokepoint (forgeable
        # platform-only keys stripped there, BEFORE the governance gate runs —
        # sanitizing here instead would nuke the gate's legitimate PII flags).
        # ``caller_keys`` may therefore include upstream-gate keys; harmless,
        # because ``set_system_value`` only consults it for the CALLER_OWNABLE
        # set (summary/tags), which no upstream platform step writes — so any
        # summary/tags present here are authentically the caller's, and
        # enrichment never clobbers them again. Platform values go through
        # ``set_system_value``: always into metadata["_system"], mirrored to
        # the legacy top-level key for one release unless caller-owned.
        metadata = data.metadata or {}
        caller_keys = frozenset(metadata.keys())
        ts_valid_start = data.ts_valid_start
        ts_valid_end = data.ts_valid_end

        # CAURA-703: record whether the CALLER chose the type, vs the enrichment
        # classifier / default fallback filling it. Captured from the raw agent
        # value BEFORE the deprecation demotion below, so an agent who supplied a
        # now-deprecated type (e.g. "semantic", coerced to fact) still counts as
        # agent-set. Fully determined at write time — the async enrichment worker
        # never overrides memory_type when the agent set it (``agent_provided_fields``
        # skip), so this flag needs no worker-side finalisation.
        memory_type_agent_set = memory_type is not None

        # CAURA-701: caller-supplied deprecated types (currently ``semantic``)
        # bypass the enrichment-LLM demotion in ``_validate_enrichment`` because
        # a non-``None`` ``data.memory_type`` short-circuits the fill-gaps branch
        # below. Demote here so the merger is enforced regardless of who chose
        # the type. Historical rows in the DB are untouched — only new writes
        # are coerced.
        if memory_type in CLASSIFIER_DEPRECATED_MEMORY_TYPES:
            memory_type = DEFAULT_MEMORY_TYPE

        if enrichment:
            # LLM fills gaps; agent-provided values always win
            if memory_type is None:
                memory_type = enrichment.memory_type
            if weight is None:
                weight = enrichment.weight
            title = enrichment.title or None
            if enrichment.summary:
                set_system_value(metadata, "summary", enrichment.summary, caller_keys=caller_keys)
            if enrichment.tags:
                set_system_value(metadata, "tags", enrichment.tags, caller_keys=caller_keys)
            if enrichment.llm_ms:
                set_system_value(metadata, "llm_ms", enrichment.llm_ms, caller_keys=caller_keys)
            # Temporal resolution: LLM-extracted dates fill gaps
            if ts_valid_start is None and enrichment.ts_valid_start:
                ts_valid_start = datetime.fromisoformat(enrichment.ts_valid_start.replace("Z", "+00:00"))
            if ts_valid_end is None and enrichment.ts_valid_end:
                ts_valid_end = datetime.fromisoformat(enrichment.ts_valid_end.replace("Z", "+00:00"))
            # PII detection
            if enrichment.contains_pii:
                set_system_value(metadata, "contains_pii", True, caller_keys=caller_keys)
                if enrichment.pii_types:
                    set_system_value(metadata, "pii_types", enrichment.pii_types, caller_keys=caller_keys)
            # Business-vs-personal classification (governance gate reads this in
            # strong mode; persisted to the row for parity with the worker path).
            set_system_value(
                metadata, "business_relevance", enrichment.business_relevance, caller_keys=caller_keys
            )

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
            set_system_value(metadata, "write_mode", resolved_write_mode, caller_keys=caller_keys)
        if resolved_write_mode == "fast" and enrichment is None:
            set_system_value(metadata, "enrichment_pending", True, caller_keys=caller_keys)

        set_system_value(metadata, "memory_type_agent_set", memory_type_agent_set, caller_keys=caller_keys)

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
