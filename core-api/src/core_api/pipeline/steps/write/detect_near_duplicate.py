"""DetectNearDuplicate — A21 advisory dedup signal on the fast write path.

Strong-mode writes run ``CheckSemanticDuplicate`` and 409-reject on a high-
similarity candidate. Default (fast / auto) writes skip semantic dedup
entirely — by design, so callers who want low-latency writes don't pay
the dedup round-trip + the LLM-judge call. But that left the asymmetry
A21 surfaced: an agent that calls in default-mode and re-states the same
fact twice ends up with two independent rows, no signal to the caller
that a near-duplicate already exists.

This step is the *advisory* twin of ``CheckSemanticDuplicate``. It runs
only on fast-mode writes, against the JUDGE band threshold (similarity ≥
``SEMANTIC_DEDUP_JUDGE_THRESHOLD = 0.85``) — wider than the strong-side
auto-reject band (``SEMANTIC_DEDUP_AUTO_THRESHOLD = 0.97``) so it catches
real paraphrases (which typically sit at cosine 0.85-0.94 once you
strip the surface lexical match) without invoking the LLM judge that
the strong path runs above this threshold. Cost: one storage round-trip
per fast write, no LLM. On hit, it stashes the candidate id on the
in-flight ``metadata`` dict (where dedup decision metadata already
lives — see ``check_semantic_duplicate.py`` for the established keys):

  metadata["near_duplicate_of"]          = "<uuid of nearest stored row>"
  metadata["near_duplicate_similarity"]  = <cosine similarity float>

The write is NOT rejected — callers continue to get a 201. They can
read the metadata back on the response to decide whether to undo, merge,
or accept the duplicate. False positives are tolerable because the
signal is advisory: an agent that mis-treats a non-duplicate as one
just loses one row's worth of context, whereas a strong-mode 409 on a
non-duplicate would have hard-rejected a legitimate write — the
LLM-judge gate exists for exactly that asymmetry, and we deliberately
skip it on the fast path. Strong-mode's 409 contract is unchanged.
"""

from __future__ import annotations

import logging
import time

from common.constants import SEMANTIC_DEDUP_JUDGE_THRESHOLD
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult
from core_api.services.dedup_identifier_filter import _content_is_identifier_bearing
from core_api.services.memory_service import _find_semantic_duplicate

logger = logging.getLogger(__name__)


class DetectNearDuplicate:
    @property
    def name(self) -> str:
        return "detect_near_duplicate"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        data = ctx.data["input"]
        tenant_config = ctx.tenant_config
        embedding = ctx.data["embedding"]
        fields = ctx.data["memory_fields"]
        metadata = fields["metadata"]

        # Same gates the strong-side check uses: respect the tenant's
        # ``semantic_dedup_enabled`` knob and skip when no embedding is
        # available (fast-mode with deferred embed, identifier-bearing
        # content, etc.).
        if not tenant_config.semantic_dedup_enabled or embedding is None:
            return StepResult(outcome=StepOutcome.SKIPPED)

        # Identifier pre-filter — mirrors ``CheckSemanticDuplicate``'s A1
        # carve-out. Content with UUID / PR-ref / build-number / semver
        # / commit-SHA tokens hits the embedder's template-slot collapse
        # at cosine ≥ 0.95 and produces false-positive near-dups.
        if _content_is_identifier_bearing(getattr(data, "content", "") or ""):
            metadata["near_dup_skipped_reason"] = "identifier_prefilter"
            return StepResult(
                outcome=StepOutcome.SKIPPED,
                detail={"reason": "identifier_prefilter"},
            )

        t_dedup = time.perf_counter()
        # JUDGE band threshold (0.85) — broad enough to catch real
        # paraphrases that the strong-side path would have surfaced
        # AND sent to the LLM judge for confirm/reject. We skip the
        # LLM call on the fast path (latency-critical), accept the
        # false-positive cost (advisory signal only, no hard reject),
        # and let callers decide what to do with the candidate.
        sem_dup = (
            await _find_semantic_duplicate(  # storage-routed (ignores db) — tolerate the db=None STM path
                data.tenant_id,
                data.fleet_id,
                embedding,
                visibility=data.visibility or "scope_team",
                min_similarity=SEMANTIC_DEDUP_JUDGE_THRESHOLD,
            )
        )
        metadata["near_dup_check_ms"] = round((time.perf_counter() - t_dedup) * 1000, 1)

        if sem_dup is None:
            return None

        sem_dup_dict = sem_dup if isinstance(sem_dup, dict) else None
        candidate_id = sem_dup_dict.get("id") if sem_dup_dict else getattr(sem_dup, "id", None)
        similarity = float(sem_dup_dict.get("similarity", 0.0)) if sem_dup_dict else 0.0

        if candidate_id is None:
            return None

        metadata["near_duplicate_of"] = str(candidate_id)
        metadata["near_duplicate_similarity"] = round(similarity, 4)
        logger.info(
            "near_duplicate_of=%s similarity=%.4f tenant_id=%s",
            candidate_id,
            similarity,
            data.tenant_id,
        )
        return None
