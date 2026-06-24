"""Fast-mode post-write governance remediation.

In fast mode (the default) enrichment is deferred to core-worker, which PATCHes
the LLM's ``contains_pii`` / ``business_relevance`` onto the already-persisted
row. The ``memclaw.memory.enriched`` consumer then calls
:func:`remediate_after_enrichment` to apply the tenant's configured action on
that free-form signal — the fast-mode counterpart to the synchronous
``GovernanceDecision`` step (strong mode).

The DETERMINISTIC pattern gate already ran synchronously pre-write
(``GovernanceScanContent``), so regex/Luhn/entropy-detectable PII/PCI/secrets
were never persisted in either mode; only the LLM's free-form judgement is
eventually-consistent here (≈ enrichment-deferral latency).
"""

from __future__ import annotations

import logging
from typing import Any

from core_api.clients.storage_client import get_storage_client
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

logger = logging.getLogger(__name__)


async def remediate_after_enrichment(memory: dict, cfg: Any) -> bool:
    """Apply LLM-signal governance to a fast-mode row after enrichment landed.

    Returns ``True`` if the row was dropped (the caller should then stop further
    processing of it). No-op + ``False`` when governance is disabled or the
    signals are clean.
    """
    pii_cfg = cfg.governance_pii
    nb_cfg = cfg.governance_non_business
    if not pii_cfg.enabled and not nb_cfg.enabled:
        return False

    md = memory.get("metadata_") or memory.get("metadata") or {}
    content = memory.get("content") or ""
    tenant_id = memory.get("tenant_id")
    agent_id = memory.get("agent_id")
    raw_id = memory.get("id")
    if raw_id is None:
        # A malformed enriched-event payload without an id would otherwise
        # soft-delete "None" and stamp resource_id="None" on every audit row.
        logger.warning("governance: remediate_after_enrichment called with memory missing 'id'; skipping")
        return False
    memory_id = str(raw_id)
    sc = get_storage_client()

    # ── PII (LLM free-form signal) ──
    if pii_cfg.enabled and md.get("contains_pii"):
        pii_types = md.get("pii_types") or []
        if pii_cfg.action == "drop":
            # Audit BEFORE the destructive delete (mirrors GovernanceScanContent's
            # audit-before-mutate): a delete that succeeds before a failing audit
            # would leave an untracked deletion in the tamper-evident log, whereas
            # an audit-then-failed-delete leaves a remediable "intended to drop" trace.
            await emit_governance_audit(
                tenant_id=tenant_id,
                agent_id=agent_id,
                action=ACTION_PII_DROP,
                detail=llm_pii_audit_detail(ACTION_PII_DROP, pii_types, content, "fast"),
                resource_id=memory_id,
                # Destructive: the soft-delete below removes the row, so this
                # audit is the only trace — must survive queue overflow.
                critical=True,
            )
            await sc.soft_delete_memory(memory_id)
            logger.info("governance: dropped fast-mode memory %s (pii)", memory_id)
            return True
        # mask/flag: the LLM gives no offsets to redact a free-form span, and in
        # fast mode the row is already persisted — so a "mask"-configured tenant
        # can only be flagged here. Keep the action truthful (flag), but record
        # the configured intent in the detail so compliance can tell this apart
        # from a genuine flag policy.
        await emit_governance_audit(
            tenant_id=tenant_id,
            agent_id=agent_id,
            action=ACTION_PII_FLAG,
            detail=llm_pii_audit_detail(
                ACTION_PII_FLAG,
                pii_types,
                content,
                "fast",
                configured_action=ACTION_PII_MASK if pii_cfg.action == "mask" else None,
            ),
            resource_id=memory_id,
        )

    # ── Business-vs-personal disposition ──
    if nb_cfg.enabled and md.get("business_relevance") == "personal":
        if nb_cfg.disposition == "drop":
            # Audit before the destructive delete (see the PII-drop branch above).
            await emit_governance_audit(
                tenant_id=tenant_id,
                agent_id=agent_id,
                action=ACTION_NB_DROP,
                detail=nonbusiness_audit_detail(ACTION_NB_DROP, content, "fast"),
                resource_id=memory_id,
                # Destructive: see the PII-drop branch — audit is the only trace.
                critical=True,
            )
            await sc.soft_delete_memory(memory_id)
            logger.info("governance: dropped fast-mode memory %s (non-business)", memory_id)
            return True
        if nb_cfg.disposition == "keep_private":
            await sc.update_memory(memory_id, tenant_id, {"visibility": "scope_agent"})
            await emit_governance_audit(
                tenant_id=tenant_id,
                agent_id=agent_id,
                action=ACTION_NB_KEEP_PRIVATE,
                detail=nonbusiness_audit_detail(ACTION_NB_KEEP_PRIVATE, content, "fast"),
                resource_id=memory_id,
            )
    return False
