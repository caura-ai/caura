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

from common.constants import SEMANTIC_DEDUP_JUDGE_THRESHOLD, SINGLE_VALUE_PREDICATES
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult
from core_api.services.dedup_identifier_filter import _content_is_identifier_bearing
from core_api.services.memory_service import _find_semantic_duplicate

logger = logging.getLogger(__name__)


def _same_claim(new, candidate: dict) -> bool:
    """True when the write restates a claim the candidate already carries.

    A71. The near-duplicate band is full of two different things: a RESTATEMENT
    of a fact whose value moved ("the deploy window is 04:00 UTC" over "…03:00
    UTC"), and a COMPLEMENTARY fact that merely reads similarly. Only the first
    should retire its predecessor; merging the second would delete information.

    The test is deterministic and costs nothing. ``EmitMemoryTriple`` has
    already run on this write and populated ``(subject_entity_id, predicate,
    object_value)`` with no LLM call, and the candidate arrives from
    ``_find_semantic_duplicate`` carrying the same columns — so both sides are
    in hand before this function is called.

    Deliberately NOT the LLM: the plan this implements describes asking "same
    claim or complementary?" as part of the enrichment call, but enrichment
    completes BEFORE the candidate is known (``ParallelEmbedEnrich`` precedes
    this step), so that phrasing would mean a SECOND model call on the latency
    path the fast mode exists to keep short. The triple answers the same
    question from data already computed.

    Three conditions, each narrowing toward "provably the same claim":

    * both sides resolved a subject and predicate — an unresolved triple proves
      nothing, and absence must never be read as a match;
    * the predicate is in ``SINGLE_VALUE_PREDICATES`` — the set where a subject
      can hold only ONE value, which is what makes a second value a replacement
      rather than an addition. "works_on" can be true of five projects at once;
      "status" cannot;
    * the objects DIFFER — an identical object is a re-statement that adds
      nothing, and superseding a row with its own content is churn. The existing
      advisory metadata already records that case.
    """
    new_subject = getattr(new, "subject_entity_id", None)
    new_predicate = getattr(new, "predicate", None)
    new_object = getattr(new, "object_value", None)
    if not new_subject or not new_predicate:
        return False
    if str(new_predicate) not in SINGLE_VALUE_PREDICATES:
        return False
    if str(new_subject) != str(candidate.get("subject_entity_id") or ""):
        return False
    if str(new_predicate) != str(candidate.get("predicate") or ""):
        return False
    cand_object = candidate.get("object_value")
    if not new_object or not cand_object:
        return False
    return str(new_object).strip().casefold() != str(cand_object).strip().casefold()


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
                # CAURA-721 — advisory here, never a 409, but the owner still
                # has to be pinned: ``near_duplicate_of`` is handed to the
                # caller as "you already recorded this", and pointing it at
                # another agent's row makes that claim false (and, for a
                # ``scope_agent`` candidate, names a row the caller cannot read).
                agent_id=getattr(data, "agent_id", None),
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

        # A71 — decide here, act after the write. This step runs BEFORE
        # ``WriteMemoryRow``, so the new row has no id yet and cannot be linked
        # to anything; ``ScheduleBackgroundTasks`` performs the supersession once
        # it does. Splitting the decision from the effect also means a merge can
        # be reasoned about (and tested) without writing rows.
        if not getattr(tenant_config, "merge_near_duplicates", False):
            return None
        # Never retire a row that is already retired, and never take a chain a
        # contradiction verdict is holding: ``outdated`` / ``conflicted`` rows
        # have an owner, and stacking a second supersession on one is how a
        # lineage forks.
        if (sem_dup_dict or {}).get("status") not in ("active", "confirmed"):
            metadata["near_dup_merge_skipped"] = "candidate_not_live"
            return None
        if not _same_claim(data, sem_dup_dict or {}):
            # The common outcome, and the one that protects information: a
            # near-duplicate that is a COMPLEMENTARY fact keeps its own row.
            metadata["near_dup_merge_skipped"] = "not_same_claim"
            return None

        ctx.data["merge_supersedes_id"] = str(candidate_id)
        metadata["near_duplicate_merged"] = True
        logger.info(
            "near_duplicate_merge subject=%s predicate=%s candidate=%s similarity=%.4f tenant_id=%s",
            getattr(data, "subject_entity_id", None),
            getattr(data, "predicate", None),
            candidate_id,
            similarity,
            data.tenant_id,
        )
        return None
