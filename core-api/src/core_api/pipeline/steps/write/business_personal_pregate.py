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
a classifier failure or a ``none`` provider never blocks a write, unless the
tenant chose ``fail_closed``.

The verdict itself is :func:`enforce_business_personal_pregate`, which the other
write shapes call too: each item of a bulk write (``create_memories_bulk``, which
ingest commit, interviews and insights use) and an edit that changes the text
(``update_memory``). The step runs it on the single create and, through
``build_enrichment_pipeline``, on the auto-chunk branch.
"""

from __future__ import annotations

import logging
from typing import Any

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


def pregate_applies(cfg: Any) -> bool:
    """Whether the tenant's pre-gate is on: non-business on, its disposition
    ``drop`` (the only one an early refusal saves anything for) and the pre-gate
    enabled. Read with ``getattr``, as the edit path reads ``governance_pii``: a
    config without the sections has no pre-gate."""
    nb = getattr(cfg, "governance_non_business", None)
    pregate = getattr(cfg, "governance_non_business_pregate", None)
    if nb is None or pregate is None:
        return False
    return bool(nb.enabled and pregate.enabled and nb.disposition == "drop")


async def enforce_business_personal_pregate(
    cfg: Any, *, content: str, tenant_id: str, agent_id: str | None, write_mode: str | None
) -> None:
    """Refuse ``content`` if the tenant's pre-gate says it is personal.

    Raises 422 on a confident "personal" verdict and 503 when no live model
    judged the text and the tenant chose ``fail_closed``; returns otherwise,
    and at once when the pre-gate is off (``pregate_applies``). Every refusal
    is written to the governance audit first, ``critical`` because the write
    it refuses leaves no other trace.
    """
    if not pregate_applies(cfg):
        return
    pregate = cfg.governance_non_business_pregate
    # Pre-gate's own provider; fall back to the tenant's enrichment provider
    # so an org can enable it without re-specifying a provider. Setting it
    # explicitly is what makes the signal independent of the enrichment one.
    provider = pregate.provider or cfg.enrichment_provider
    result = await classify_business_personal(content, cfg, provider=provider, model=pregate.model)

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
                tenant_id,
                provider,
            )
            await emit_governance_audit(
                tenant_id=tenant_id,
                agent_id=agent_id,
                action=ACTION_NB_PREGATE_UNAVAILABLE,
                detail=nonbusiness_pregate_audit_detail(
                    ACTION_NB_PREGATE_UNAVAILABLE,
                    content,
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
            tenant_id,
            provider,
        )
        return

    # Fail-open: only a confident "personal" verdict blocks. min_confidence
    # None → 0.0 → act on any "personal" (matches the post-enrichment gate).
    threshold = pregate.min_confidence or 0.0
    if result.business_relevance == "personal" and result.confidence >= threshold:
        await emit_governance_audit(
            tenant_id=tenant_id,
            agent_id=agent_id,
            action=ACTION_NB_PREGATE_DROP,
            detail=nonbusiness_pregate_audit_detail(
                ACTION_NB_PREGATE_DROP,
                content,
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


class BusinessPersonalPregate:
    @property
    def name(self) -> str:
        return "business_personal_pregate"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        cfg = ctx.tenant_config
        if cfg is None or not pregate_applies(cfg):
            return StepResult(outcome=StepOutcome.SKIPPED)
        data = ctx.data["input"]
        # An extract-only preview (``persist=False``, which shares
        # ``build_enrichment_pipeline`` with auto-chunk) stores nothing and
        # hands the caller back its own text: nothing to refuse.
        if not getattr(data, "persist", True):
            return StepResult(outcome=StepOutcome.SKIPPED)
        await enforce_business_personal_pregate(
            cfg,
            content=data.content,
            tenant_id=data.tenant_id,
            agent_id=data.agent_id,
            write_mode=ctx.data.get("resolved_write_mode"),
        )
        return None
