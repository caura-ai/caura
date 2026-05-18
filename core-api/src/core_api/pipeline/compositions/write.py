"""Write pipeline compositions — enrichment + persist phases."""

from core_api.pipeline.runner import Pipeline
from core_api.pipeline.steps.write import (
    CheckContentLength,
    CheckExactDuplicate,
    CheckSemanticDuplicate,
    ComputeContentHash,
    EmitMemoryTriple,
    LoadTenantConfig,
    MergeEnrichmentFields,
    ParallelEmbedEnrich,
    ResolveSTMTarget,
    ScheduleBackgroundTasks,
    WriteMemoryRow,
    WriteSTMNote,
)


def build_enrichment_pipeline() -> Pipeline:
    """Always runs (needed by all branches: persist, extract-only, auto-chunk)."""
    return Pipeline(
        "write_enrichment",
        [
            CheckContentLength(),
            LoadTenantConfig(),
            ComputeContentHash(),
            ParallelEmbedEnrich(),
            MergeEnrichmentFields(),
        ],
    )


def build_persist_pipeline() -> Pipeline:
    """Only for persist=True, non-chunked memories."""
    return Pipeline(
        "write_persist",
        [
            # entity_links and content are expected to be fully enriched by the
            # upstream enrichment pipeline before this path runs.
            EmitMemoryTriple(),
            CheckExactDuplicate(),
            CheckSemanticDuplicate(),
            WriteMemoryRow(),
            ScheduleBackgroundTasks(),
        ],
    )


def build_fast_write_pipeline() -> Pipeline:
    """Fast write mode: enrichment + exact-dedup + write (skips semantic dedup)."""
    return Pipeline(
        "write_fast",
        [
            CheckContentLength(),
            LoadTenantConfig(),
            ComputeContentHash(),
            ParallelEmbedEnrich(),
            MergeEnrichmentFields(),
            EmitMemoryTriple(),
            CheckExactDuplicate(),
            WriteMemoryRow(),
            ScheduleBackgroundTasks(),
        ],
    )


def build_stm_write_pipeline() -> Pipeline:
    """STM write mode: validate content, resolve target, write to STM backend."""
    return Pipeline(
        "write_stm",
        [
            CheckContentLength(),
            ResolveSTMTarget(),
            WriteSTMNote(),
        ],
    )


def build_strong_write_pipeline() -> Pipeline:
    """Strong write mode: full enrichment + exact + semantic dedup + write."""
    return Pipeline(
        "write_strong",
        [
            CheckContentLength(),
            LoadTenantConfig(),
            ComputeContentHash(),
            ParallelEmbedEnrich(),
            MergeEnrichmentFields(),
            EmitMemoryTriple(),
            CheckExactDuplicate(),
            CheckSemanticDuplicate(),
            WriteMemoryRow(),
            ScheduleBackgroundTasks(),
        ],
    )
