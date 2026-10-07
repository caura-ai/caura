"""Deterministic PII policy for memory content edits.

The create and bulk paths apply the tenant's ``governance.pii`` action before
anything is stored (``pipeline/steps/write/governance_scan_content.py`` and the
bulk inline gate). ``PATCH /memories/{id}`` and MCP ``op=update`` did not, so
content a create would have masked or refused was stored as sent on an edit,
with no governance audit row.
"""

from __future__ import annotations

from fastapi import HTTPException

from common.governance import mask, scan
from core_api.services.governance_gate import (
    ACTION_PII_DROP,
    ACTION_PII_FLAG,
    ACTION_PII_MASK,
    emit_governance_audit,
    mark_pii_flagged,
    pii_audit_detail,
)

_WRITE_MODE = "update"


async def apply_pii_policy_to_update(
    *, tenant_id: str, agent_id: str | None, content: str, gov
) -> tuple[str, dict]:
    """Return ``(content_to_store, metadata_flags)`` for an edited memory.

    ``drop`` raises 422 (nothing is persisted); ``mask`` returns redacted
    content; ``flag`` returns the metadata keys to merge into the row. Every
    action is written to the governance audit, as on create.
    """
    if gov is None or not gov.enabled or not content:
        return content, {}
    findings = scan(content, enabled_categories=gov.enabled_categories)
    if not findings:
        return content, {}
    if gov.action == "drop":
        await emit_governance_audit(
            tenant_id=tenant_id,
            agent_id=agent_id,
            action=ACTION_PII_DROP,
            detail=pii_audit_detail(ACTION_PII_DROP, findings, content, _WRITE_MODE),
            critical=True,
        )
        raise HTTPException(
            status_code=422, detail="Memory rejected by content policy: sensitive data detected"
        )
    if gov.action == "mask":
        await emit_governance_audit(
            tenant_id=tenant_id,
            agent_id=agent_id,
            action=ACTION_PII_MASK,
            detail=pii_audit_detail(ACTION_PII_MASK, findings, content, _WRITE_MODE),
        )
        return mask(content, findings), {}
    flags: dict = {}
    mark_pii_flagged(flags, findings)
    await emit_governance_audit(
        tenant_id=tenant_id,
        agent_id=agent_id,
        action=ACTION_PII_FLAG,
        detail=pii_audit_detail(ACTION_PII_FLAG, findings, content, _WRITE_MODE),
    )
    return content, flags
