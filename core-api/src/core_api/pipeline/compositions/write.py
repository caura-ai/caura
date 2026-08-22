"""Write pipeline compositions — enrichment + persist phases."""

from core_api.pipeline.runner import Pipeline
from core_api.pipeline.steps.write import (
    BusinessPersonalPregate,
    CheckContentLength,
    CheckExactDuplicate,
    CheckSemanticDuplicate,
    ComputeContentHash,
    DetectNearDuplicate,
    EmitMemoryTriple,
    GovernanceDecision,
    GovernanceScanContent,
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
            # Deterministic PII gate BEFORE the content hash so mask/drop act on
            # — and the hash/dedup see — the redacted content (eToro governance).
            GovernanceScanContent(),
            ComputeContentHash(),
            ParallelEmbedEnrich(),
            MergeEnrichmentFields(),
        ],
    )


def build_auto_chunk_governance_pipeline() -> Pipeline:
    """The LLM verdict gate for the auto-chunk branch (#852).

    ``GovernanceDecision`` cannot simply be appended to
    ``build_enrichment_pipeline``: that composition is shared with the
    extract-only branch (``persist=False``), which writes nothing and returns a
    preview of content the caller already holds. Rejecting there would refuse a
    request that leaks nothing. So the step is applied only on the branch that
    goes on to persist.

    A composition rather than a bare ``GovernanceDecision().execute(ctx)`` call
    so that "which write paths enforce the LLM verdict?" stays answerable by
    reading this module — the question that got the wrong answer when the
    auto-chunk branch was written.
    """
    return Pipeline("write_auto_chunk_governance", [GovernanceDecision()])


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
    """Fast write mode: enrichment + exact-dedup + advisory near-dup detect + write.

    Distinct from strong-mode: ``DetectNearDuplicate`` (A21) is advisory —
    it stashes ``metadata["near_duplicate_of"]`` and ``metadata["near_duplicate_similarity"]``
    on a high-similarity hit but does NOT 409-reject the write. Strong-mode
    keeps its 409 contract via ``CheckSemanticDuplicate``. Net: agents
    using fast-mode can now detect "I just re-stated the same fact"
    without paying the strong-mode dedup latency / rejection.
    """
    return Pipeline(
        "write_fast",
        [
            CheckContentLength(),
            LoadTenantConfig(),
            GovernanceScanContent(),
            # Opt-in fast business/personal go/no-go: reject personal content
            # (disposition=drop) before enrichment/embedding/extraction run.
            BusinessPersonalPregate(),
            ComputeContentHash(),
            ParallelEmbedEnrich(),
            MergeEnrichmentFields(),
            EmitMemoryTriple(),
            CheckExactDuplicate(),
            DetectNearDuplicate(),
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
            # Deterministic PII gate also guards STM (ephemeral notes still
            # shouldn't persist secrets). STM bypasses enrichment, so only the
            # deterministic scan applies — no LLM signal.
            GovernanceScanContent(),
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
            GovernanceScanContent(),
            # Opt-in fast business/personal go/no-go: reject personal content
            # (disposition=drop) before enrichment/embedding/extraction run.
            BusinessPersonalPregate(),
            ComputeContentHash(),
            ParallelEmbedEnrich(),
            MergeEnrichmentFields(),
            # Strong mode runs enrichment inline, so the LLM's free-form PII +
            # business/personal signal is available pre-persist. Acts on it
            # before dedup/write (fast mode does this as post-write remediation).
            GovernanceDecision(),
            EmitMemoryTriple(),
            CheckExactDuplicate(),
            CheckSemanticDuplicate(),
            WriteMemoryRow(),
            ScheduleBackgroundTasks(),
        ],
    )
