"""BusinessPersonalPregate — fast business/personal go/no-go BEFORE enrichment.

Opt-in (``governance.non_business.pregate.enabled``) and only active when the
tenant's non-business disposition is ``drop``. Positioned right after the
deterministic ``GovernanceScanContent`` and before ``ComputeContentHash`` /
``ParallelEmbedEnrich``, so a confident "personal" verdict rejects the write
with 422 *before* the expensive enrichment, embedding, and (downstream)
background entity extraction run — and before any row is written.

``keep_private`` / ``store`` dispositions persist the row, so an early exit
saves nothing; they stay with the post-enrichment ``GovernanceDecision`` (which
also remains the accurate backstop when the pre-gate says "business"). Fail-open:
a classifier failure or a ``none`` provider never blocks a write.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult
from core_api.services.business_classifier import classify_business_personal
from core_api.services.governance_gate import (
    ACTION_NB_PREGATE_DROP,
    ACTION_NB_PREGATE_UNAVAILABLE,
    emit_governance_audit,
    nonbusiness_pregate_audit_detail,
)

logger = logging.getLogger(__name__)


class BusinessPersonalPregate:
    @property
    def name(self) -> str:
        return "business_personal_pregate"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        cfg = ctx.tenant_config
        if cfg is None:
            return StepResult(outcome=StepOutcome.SKIPPED)
        nb = cfg.governance_non_business
        pregate = cfg.governance_non_business_pregate
        # Only a "drop" disposition benefits from an early gate.
        if not (nb.enabled and pregate.enabled and nb.disposition == "drop"):
            return StepResult(outcome=StepOutcome.SKIPPED)

        data = ctx.data["input"]
        write_mode = ctx.data.get("resolved_write_mode")
        # Pre-gate's own provider; fall back to the tenant's enrichment provider
        # so an org can enable it without re-specifying a provider. Setting it
        # explicitly is what makes the signal independent of the enrichment one.
        provider = pregate.provider or cfg.enrichment_provider
        result = await classify_business_personal(data.content, cfg, provider=provider, model=pregate.model)

        # No live model evaluated this write (``provider is None`` ⇔ the
        # fail-open path: ``none`` provider, timeout, or classifier error), so a
        # "drop" policy isn't actually being enforced on it. Act per the
        # tenant's fail-open (default) vs fail-closed choice.
        if result.provider is None:
            if pregate.fail_closed:
                # Fail-closed: reject rather than store unclassified content.
                logger.warning(
                    "business_personal_pregate: classifier unavailable while a non-business "
                    "'drop' policy is active — REJECTING write (fail_closed) (tenant=%s, provider=%s)",
                    data.tenant_id,
                    provider,
                )
                await emit_governance_audit(
                    ctx.db,
                    tenant_id=data.tenant_id,
                    agent_id=data.agent_id,
                    action=ACTION_NB_PREGATE_UNAVAILABLE,
                    detail=nonbusiness_pregate_audit_detail(
                        ACTION_NB_PREGATE_UNAVAILABLE,
                        data.content,
                        write_mode,
                        provider=provider,
                        model=pregate.model,
                        confidence=None,
                    ),
                    critical=True,
                )
                raise HTTPException(
                    status_code=503,
                    detail="Content policy classifier unavailable; write rejected (fail-closed)",
                )
            # Fail-open (default): store the write but surface the silent bypass
            # so a compliance tenant can detect it (the post-enrichment gate is
            # the backstop).
            logger.warning(
                "business_personal_pregate: classifier unavailable (fail-open) while a "
                "non-business 'drop' policy is active — content stored WITHOUT pre-gate "
                "enforcement (tenant=%s, provider=%s)",
                data.tenant_id,
                provider,
            )
            return None

        # Fail-open: only a confident "personal" verdict blocks. min_confidence
        # None → 0.0 → act on any "personal" (matches the post-enrichment gate).
        threshold = pregate.min_confidence or 0.0
        if result.business_relevance == "personal" and result.confidence >= threshold:
            await emit_governance_audit(
                ctx.db,
                tenant_id=data.tenant_id,
                agent_id=data.agent_id,
                action=ACTION_NB_PREGATE_DROP,
                detail=nonbusiness_pregate_audit_detail(
                    ACTION_NB_PREGATE_DROP,
                    data.content,
                    write_mode,
                    # The provider/model that ACTUALLY classified (may be the
                    # fallback) — not the intended ones — so audit is truthful.
                    provider=result.provider,
                    model=result.model,
                    confidence=result.confidence,
                ),
                # Reject path: the write is refused, so its audit must not be
                # lost to queue overflow — sync-fallback rather than drop.
                critical=True,
            )
            raise HTTPException(
                status_code=422,
                detail="Memory rejected by content policy: non-business content",
            )
        return None
