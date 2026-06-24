"""GovernanceScanContent — deterministic PII/secret gate at the ingestion boundary.

Runs in BOTH write modes, positioned after ``LoadTenantConfig`` and before
``ComputeContentHash``: it scans ``data.content`` with the deterministic pattern
library and, per the tenant's configured action, masks the content IN PLACE
(so the hash, dedup, embedding, and stored row all see the redacted form — the
mask-vs-content-hash ordering is solved structurally), drops the write (raises
422 before anything persists), or flags it in metadata. Every action is
recorded to the tamper-evident audit log.

This is the always-on enforcement path: regex/Luhn/IBAN/entropy-detectable
PII/PCI/secrets are governed before persistence regardless of write mode, which
is also the fail-closed backstop for high-risk patterns (the validators are
deterministic — never "uncertain"). The LLM's free-form PII signal is applied
separately by ``GovernanceDecision`` (strong mode) / the post-write remediation
(fast mode).
"""

from __future__ import annotations

from fastapi import HTTPException

from common.governance import mask, scan
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult
from core_api.services.governance_gate import (
    ACTION_PII_DROP,
    ACTION_PII_FLAG,
    ACTION_PII_MASK,
    emit_governance_audit,
    mark_pii_flagged,
    pii_audit_detail,
)


class GovernanceScanContent:
    @property
    def name(self) -> str:
        return "governance_scan_content"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        data = ctx.data["input"]
        cfg = ctx.tenant_config
        gov = cfg.governance_pii if cfg is not None else None
        if gov is None or not gov.enabled:
            return StepResult(outcome=StepOutcome.SKIPPED)
        if not data.content:
            return None

        findings = scan(data.content, enabled_categories=gov.enabled_categories)
        if not findings:
            return None

        write_mode = ctx.data.get("resolved_write_mode")
        if gov.action == "drop":
            # Audit BEFORE raising (resource_id unknown — nothing persists).
            await emit_governance_audit(
                tenant_id=data.tenant_id,
                agent_id=data.agent_id,
                action=ACTION_PII_DROP,
                detail=pii_audit_detail(ACTION_PII_DROP, findings, data.content, write_mode),
                # Reject path: the write is refused, so this audit is the only
                # record — must survive queue overflow (sync-fallback, not drop).
                critical=True,
            )
            raise HTTPException(
                status_code=422,
                detail="Memory rejected by content policy: sensitive data detected",
            )

        if gov.action == "mask":
            # Audit with the original content's findings/hash, THEN mutate in
            # place so every downstream consumer (hash, dedup, embed, store)
            # sees the masked form.
            await emit_governance_audit(
                tenant_id=data.tenant_id,
                agent_id=data.agent_id,
                action=ACTION_PII_MASK,
                detail=pii_audit_detail(ACTION_PII_MASK, findings, data.content, write_mode),
            )
            data.content = mask(data.content, findings)
            return None

        # flag: store with a warning in metadata (staged, audit-first rollout).
        metadata = data.metadata or {}
        mark_pii_flagged(metadata, findings)
        data.metadata = metadata
        await emit_governance_audit(
            tenant_id=data.tenant_id,
            agent_id=data.agent_id,
            action=ACTION_PII_FLAG,
            detail=pii_audit_detail(ACTION_PII_FLAG, findings, data.content, write_mode),
        )
        return None
