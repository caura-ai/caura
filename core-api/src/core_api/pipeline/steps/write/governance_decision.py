"""GovernanceDecision — apply the LLM's free-form PII + business/personal signal.

Strong-mode only: enrichment runs inline before ``WriteMemoryRow``, so the
LLM's ``contains_pii`` / ``business_relevance`` are available pre-persist. (Fast
mode defers enrichment to the worker, so the equivalent runs as the post-write
remediation in ``services/governance_remediation``.) Positioned after
``MergeEnrichmentFields``, before the dedup/write steps.

The deterministic ``GovernanceScanContent`` step already masked pattern-matched
PII pre-hash; this handles only what patterns can't: the LLM's free-form PII
judgement (no span offsets, so mask-mode falls back to flagging the row) and the
business-vs-personal disposition. Under LLM uncertainty (enrichment unavailable
/ heuristic fallback) it takes no destructive action — the deterministic step is
the high-risk fail-closed backstop — and records the uncertainty for audit.
"""

from __future__ import annotations

from fastapi import HTTPException

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult
from core_api.services.governance_gate import (
    ACTION_NB_DROP,
    ACTION_NB_KEEP_PRIVATE,
    ACTION_PII_DROP,
    ACTION_PII_FLAG,
    ACTION_PII_MASK,
    emit_governance_audit,
    llm_pii_audit_detail,
    nonbusiness_audit_detail,
)


class GovernanceDecision:
    @property
    def name(self) -> str:
        return "governance_decision"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        cfg = ctx.tenant_config
        if cfg is None:
            return StepResult(outcome=StepOutcome.SKIPPED)
        pii_cfg = cfg.governance_pii
        nb_cfg = cfg.governance_non_business
        if not pii_cfg.enabled and not nb_cfg.enabled:
            return StepResult(outcome=StepOutcome.SKIPPED)

        data = ctx.data["input"]
        enr = ctx.data.get("enrichment")
        fields = ctx.data.get("memory_fields") or {}
        metadata = fields.get("metadata")
        if metadata is None:
            metadata = data.metadata or {}
            data.metadata = metadata  # mirror governance_scan_content; keep mutations
        write_mode = ctx.data.get("resolved_write_mode")

        # Uncertain signal (enrichment unavailable or heuristic fallback): the
        # deterministic step already handled high-risk patterns; record the
        # uncertainty and take no destructive action on an absent signal.
        if enr is None or getattr(enr, "llm_ms", 0) == 0:
            metadata["governance_llm_uncertain"] = True
            return None

        # ── PII (LLM free-form signal) ──
        if pii_cfg.enabled and enr.contains_pii:
            if pii_cfg.action == "drop":
                await emit_governance_audit(
                    ctx.db,
                    tenant_id=data.tenant_id,
                    agent_id=data.agent_id,
                    action=ACTION_PII_DROP,
                    detail=llm_pii_audit_detail(ACTION_PII_DROP, enr.pii_types, data.content, write_mode),
                )
                raise HTTPException(
                    status_code=422,
                    detail="Memory rejected by content policy: sensitive data detected",
                )
            # mask or flag: the LLM gives no offsets to redact a free-form span,
            # so record the PII judgement on the row (flag) either way — but keep
            # the configured intent in the detail so a "mask" policy that could
            # only flag here stays distinguishable from a genuine flag policy.
            metadata["contains_pii"] = True
            if enr.pii_types:
                metadata["pii_types"] = enr.pii_types
            await emit_governance_audit(
                ctx.db,
                tenant_id=data.tenant_id,
                agent_id=data.agent_id,
                action=ACTION_PII_FLAG,
                detail=llm_pii_audit_detail(
                    ACTION_PII_FLAG,
                    enr.pii_types,
                    data.content,
                    write_mode,
                    configured_action=ACTION_PII_MASK if pii_cfg.action == "mask" else None,
                ),
            )

        # ── Business-vs-personal disposition ──
        if nb_cfg.enabled and getattr(enr, "business_relevance", "business") == "personal":
            if nb_cfg.disposition == "drop":
                await emit_governance_audit(
                    ctx.db,
                    tenant_id=data.tenant_id,
                    agent_id=data.agent_id,
                    action=ACTION_NB_DROP,
                    detail=nonbusiness_audit_detail(ACTION_NB_DROP, data.content, write_mode),
                )
                raise HTTPException(
                    status_code=422,
                    detail="Memory rejected by content policy: non-business content",
                )
            if nb_cfg.disposition == "keep_private":
                # Retain only in the creating agent's scope, invisible to team/fleet.
                data.visibility = "scope_agent"
                metadata["nonbusiness_kept_private"] = True
                await emit_governance_audit(
                    ctx.db,
                    tenant_id=data.tenant_id,
                    agent_id=data.agent_id,
                    action=ACTION_NB_KEEP_PRIVATE,
                    detail=nonbusiness_audit_detail(ACTION_NB_KEEP_PRIVATE, data.content, write_mode),
                )
            # store: no-op (classification already recorded in metadata)
        return None
