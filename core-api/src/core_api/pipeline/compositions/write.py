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


def _persist_steps(semantic_gate) -> list:
    """The persist tail shared by the strong and fast auto-chunk fall-throughs.

    The two differ in exactly one step — the semantic gate. (The inline
    pipelines differ in that step too, and additionally in
    ``GovernanceDecision``, which neither fall-through has.) Spelling the
    difference once keeps them from drifting apart the way they can when each is
    written out in full.

    ``CheckExactDuplicate`` leads, for the reason given at the same move in
    ``build_strong_write_pipeline``.
    """
    return [
        CheckExactDuplicate(),
        # entity_links and content are expected to be fully enriched by the
        # upstream enrichment pipeline before this path runs.
        EmitMemoryTriple(),
        semantic_gate,
        WriteMemoryRow(),
        ScheduleBackgroundTasks(),
    ]


def build_persist_pipeline() -> Pipeline:
    """Only for persist=True, non-chunked memories. Strong-mode dedup contract."""
    return Pipeline("write_persist", _persist_steps(CheckSemanticDuplicate()))


def build_fast_persist_pipeline() -> Pipeline:
    """``build_persist_pipeline`` with fast mode's dedup contract (OSS 09/02 M-47).

    The auto-chunk branch reaches a persist pipeline when chunking yields 0-1
    facts, and it used to reach ``build_persist_pipeline`` — the strong one —
    whatever the caller asked for. A ``write_mode="fast"`` write of >2000 chars
    could therefore be 409-rejected by ``CheckSemanticDuplicate``, a gate fast
    mode exists to opt out of, purely because the content was long enough to
    attempt chunking and the chunker found nothing to split.

    Same substitution fast mode makes on the inline path: the advisory
    ``DetectNearDuplicate`` marks the row and lets the write land.
    """
    return Pipeline("write_fast_persist", _persist_steps(DetectNearDuplicate()))


def build_auto_chunk_dedup_pipeline() -> Pipeline:
    """The auto-chunk PARENT's semantic dedup gate (OSS 08/14 M-16).

    The multi-fact branch writes its parent row by hand rather than through a
    persist pipeline, and so had no semantic gate at all: re-submitting the same
    long document produced a second parent whenever the content hash differed by
    a byte. The children have had an exact-hash gate since #841; this is the
    parent's.

    Runs the SAME step object the strong inline pipeline uses rather than
    reimplementing the thresholds — ``CheckSemanticDuplicate`` reads only
    ``input``/``embedding``/``memory_fields``/``tenant_config``, all of which the
    auto-chunk context already carries, and it skips itself when the embedding is
    absent (deferred deployments) exactly as it does inline.

    STRONG MODE ONLY — and the gap that leaves is worth naming rather than
    reading as settled design. Fast mode's gate is ``DetectNearDuplicate``,
    which does not refuse the write: on a hit it RECORDS a merge intent in
    ``ctx.data["merge_supersedes_id"]`` and stamps
    ``metadata["near_duplicate_merged"] = True``, and it is
    ``ScheduleBackgroundTasks`` that later performs the merge against the new
    row's id. The multi-fact branch has no ``ScheduleBackgroundTasks`` — it
    writes the parent itself — so running that step here would stamp a parent
    merged while no merge ever happened, which is worse than no gate.

    Doing it properly is not blocked on plumbing (``_merge_near_duplicate`` is
    importable and takes ids) but on semantics nobody has settled: superseding
    an auto-chunked parent leaves the superseded parent's children live and
    unowned, and ``supersedes_id`` has no defined meaning across a parent/child
    set. Until that is answered, a fast write's near-duplicate handling depends
    on how many facts the chunker found — present at 0-1, absent at 2+ — which
    is the same shape of inconsistency M-47 just removed from the mode
    resolution, one level down. Left open deliberately, not fixed.

    A composition, for the reason ``build_auto_chunk_governance_pipeline`` gives:
    "which write paths enforce the dedup contract?" should stay answerable by
    reading this module. That question got the wrong answer here for both gates.
    """
    return Pipeline("write_auto_chunk_dedup", [CheckSemanticDuplicate()])


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
            # Before the embed/enrich pair, not after it — see
            # ``build_strong_write_pipeline`` for the full reasoning. Fast mode
            # has no ``GovernanceDecision``, so here the move is pure: nothing
            # between the hash and the gate has a side effect a duplicate would
            # otherwise have been charged for.
            CheckExactDuplicate(),
            ParallelEmbedEnrich(),
            MergeEnrichmentFields(),
            EmitMemoryTriple(),
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
            # OSS 08/14 L-33 — the exact-hash gate runs as early as its input
            # allows, which is the statement right after the hash exists. It
            # reads nothing but ``content_hash``, so every step it used to sit
            # behind was work a duplicate paid for and then threw away: an inline
            # LLM enrichment call, an embedding call, and an entity UPSERT in
            # ``EmitMemoryTriple``.
            #
            # One behaviour change, and it is deliberate: a write whose content
            # duplicates an existing row is now refused with 409 WITHOUT running
            # ``GovernanceDecision``, so the LLM free-form verdict and its audit
            # event no longer fire for it. The deterministic gate
            # (``GovernanceScanContent``) still runs on every write — it is ahead
            # of the hash precisely so mask/drop act on the content that gets
            # hashed — and the row being duplicated was itself governed when it
            # was written. What is lost is a second verdict on content already
            # stored; what is bought is not paying for an LLM call on every
            # retry. Fast mode makes the same trade with nothing to weigh, since
            # it has no ``GovernanceDecision`` step.
            CheckExactDuplicate(),
            ParallelEmbedEnrich(),
            MergeEnrichmentFields(),
            # Strong mode runs enrichment inline, so the LLM's free-form PII +
            # business/personal signal is available pre-persist. Acts on it
            # before dedup/write (fast mode does this as post-write remediation).
            GovernanceDecision(),
            EmitMemoryTriple(),
            CheckSemanticDuplicate(),
            WriteMemoryRow(),
            ScheduleBackgroundTasks(),
        ],
    )
