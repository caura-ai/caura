"""CheckSemanticDuplicate — two-tier dedup gate (A1 #16).

Decision band (cosine similarity to nearest stored memory; see
``common.constants`` for the thresholds added in A1 #15):

  similarity ≥ SEMANTIC_DEDUP_AUTO_THRESHOLD  → 409 auto-reject (no LLM)
  JUDGE ≤ sim < AUTO                          → LLM judge decides
  similarity < SEMANTIC_DEDUP_JUDGE_THRESHOLD → accept (no candidate
                                                surfaced from storage)

The judge call is gated on ``DEDUP_JUDGE_CONFIDENCE_THRESHOLD`` so a
malformed/heuristic-fallback response (confidence 0.50) cannot 409 a
legitimate write — only a confident "this IS a duplicate" call does.
"""

from __future__ import annotations

import logging
import time

from fastapi import HTTPException

from common.constants import (
    SEMANTIC_DEDUP_AUTO_THRESHOLD,
    SEMANTIC_DEDUP_JUDGE_THRESHOLD,
)
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult
from core_api.services.dedup_judge import (
    DEDUP_JUDGE_CONFIDENCE_THRESHOLD,
    _llm_dedup_check,
)
from core_api.services.memory_service import _find_semantic_duplicate

logger = logging.getLogger(__name__)


class CheckSemanticDuplicate:
    @property
    def name(self) -> str:
        return "check_semantic_duplicate"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        data = ctx.data["input"]
        tenant_config = ctx.tenant_config
        embedding = ctx.data["embedding"]
        fields = ctx.data["memory_fields"]
        metadata = fields["metadata"]

        if not tenant_config.semantic_dedup_enabled or embedding is None:
            return StepResult(outcome=StepOutcome.SKIPPED)

        t_dedup = time.perf_counter()
        # Surface candidates down to the JUDGE band so this step can
        # decide auto-reject vs judge-dispatch vs accept by tier.
        sem_dup = await _find_semantic_duplicate(
            ctx.require_db,
            data.tenant_id,
            data.fleet_id,
            embedding,
            visibility=data.visibility or "scope_team",
            min_similarity=SEMANTIC_DEDUP_JUDGE_THRESHOLD,
        )
        metadata["semantic_dedup_ms"] = round((time.perf_counter() - t_dedup) * 1000, 1)

        if sem_dup is None:
            return None

        sem_dup_dict = sem_dup if isinstance(sem_dup, dict) else None
        candidate_id = sem_dup_dict.get("id") if sem_dup_dict else getattr(sem_dup, "id", None)
        similarity = float(sem_dup_dict.get("similarity", 0.0)) if sem_dup_dict else 0.0

        if similarity >= SEMANTIC_DEDUP_AUTO_THRESHOLD:
            # Auto-reject band — no LLM call.
            raise HTTPException(
                status_code=409,
                detail=f"Near-duplicate memory exists: {candidate_id}",
            )

        # Judge band — dispatch the LLM judge with A4 #12's
        # (verdict, confidence) shape via ``_llm_dedup_check``.
        candidate_content = sem_dup_dict.get("content", "") if sem_dup_dict else ""
        new_content = data.content if hasattr(data, "content") else ""

        t_judge = time.perf_counter()
        is_dup, confidence = await _llm_dedup_check(new_content, candidate_content, tenant_config)
        metadata["dedup_judge_ms"] = round((time.perf_counter() - t_judge) * 1000, 1)
        metadata["dedup_judge_confidence"] = confidence
        metadata["dedup_candidate_similarity"] = similarity

        if is_dup and confidence >= DEDUP_JUDGE_CONFIDENCE_THRESHOLD:
            raise HTTPException(
                status_code=409,
                detail=f"Near-duplicate memory exists: {candidate_id}",
            )

        # Either judge said not a duplicate, or said duplicate at low
        # confidence — accept the write.
        return None
