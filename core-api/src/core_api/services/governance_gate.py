"""Shared governance-gate helpers: PII-safe audit-detail builders + emit.

Used by the write-path steps (``GovernanceScanContent`` / ``GovernanceDecision``),
the fast-mode post-write remediation, and the bulk path. Audit details are
built from :class:`~common.governance.Finding` offsets/categories only — never
the raw matched text — so they can't leak the secret they record (the
storage-side ``assert_pii_safe`` guard is the defense-in-depth backstop).
"""

from __future__ import annotations

import hashlib

from common.governance import Finding
from core_api.services.audit_service import log_action

# Audit action verbs (also the prefixes the governance UI filters list views on).
ACTION_PII_MASK = "pii_mask"
ACTION_PII_DROP = "pii_drop"
ACTION_PII_FLAG = "pii_flag"
ACTION_NB_DROP = "nonbusiness_drop"
ACTION_NB_KEEP_PRIVATE = "nonbusiness_keep_private"
# Pre-gate drop: rejected by the fast business/personal classifier BEFORE any
# persistence. Distinct from ``nonbusiness_drop`` (which in fast-mode remediation
# soft-deletes an already-stored row) — the verb must describe what physically
# happened, and here nothing was ever written.
ACTION_NB_PREGATE_DROP = "nonbusiness_pregate_drop"
# Pre-gate could not classify (provider ``none`` / timeout / error) while a
# "drop" policy was active AND the tenant opted into fail-closed, so the write
# was rejected (503) rather than stored unclassified. Distinct from a drop
# (nothing was classified) — records the unavailability-turned-rejection.
ACTION_NB_PREGATE_UNAVAILABLE = "nonbusiness_pregate_unavailable"


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _base_audit_detail(action: str, content: str, write_mode: str | None) -> dict:
    """Fields common to every governance audit detail: the action, a PII-safe
    content hash, and the write mode. Specialized builders add evidence on top."""
    return {"action": action, "content_sha256": _content_sha256(content), "write_mode": write_mode}


def pii_audit_detail(action: str, findings: list[Finding], content: str, write_mode: str | None) -> dict:
    """PII-safe audit detail: categories + severities + span offsets + a content
    hash — never the matched values."""
    detail = _base_audit_detail(action, content, write_mode)
    detail.update(
        {
            "categories": sorted({f.category.value for f in findings}),
            "severities": sorted({f.severity.value for f in findings}),
            "spans": [[f.start, f.end] for f in findings],
            "finding_count": len(findings),
        }
    )
    return detail


def llm_pii_audit_detail(
    action: str,
    pii_types: list[str],
    content: str,
    write_mode: str | None,
    configured_action: str | None = None,
) -> dict:
    """Audit detail for the LLM's free-form PII signal (no span offsets — the
    LLM reports categories, not positions). Records the category labels + a
    content hash; never raw values.

    ``configured_action`` records the tenant's intended action when it differs
    from the one taken. The LLM signal has no span offsets, so a ``mask`` policy
    can't redact a free-form match and falls back to flagging the row; recording
    the intent lets a compliance review distinguish that fallback from a genuine
    ``flag`` policy — without an action verb that claims a redaction never
    happened (``pii_mask`` means content was actually masked, only the
    deterministic span-aware path can honestly emit it)."""
    detail = _base_audit_detail(action, content, write_mode)
    detail.update({"categories": sorted(set(pii_types or [])), "source": "llm"})
    if configured_action is not None and configured_action != action:
        detail["configured_action"] = configured_action
    return detail


def nonbusiness_audit_detail(action: str, content: str, write_mode: str | None) -> dict:
    return _base_audit_detail(action, content, write_mode)


def nonbusiness_pregate_audit_detail(
    action: str,
    content: str,
    write_mode: str | None,
    *,
    provider: str | None,
    model: str | None,
    confidence: float | None,
) -> dict:
    """Audit detail for the fast pre-gate's business/personal decision. Records
    the classifier provider/model + confidence (metadata only — never the raw
    content) so a compliance review can see what made the early-reject call."""
    detail = _base_audit_detail(action, content, write_mode)
    detail.update({"source": "llm_pregate", "provider": provider, "model": model, "confidence": confidence})
    return detail


def mark_pii_flagged(metadata: dict, findings: list[Finding]) -> None:
    """Record a deterministic PII flag on ``metadata`` in place (shared by the
    write-path scan step and the bulk gate so the flag shape stays consistent)."""
    metadata["contains_pii"] = True
    metadata["pii_types"] = sorted({f.category.value for f in findings})
    metadata["pii_flagged_by"] = "governance.deterministic"


async def emit_governance_audit(
    *,
    tenant_id: str,
    agent_id: str | None,
    action: str,
    detail: dict,
    resource_id: str | None = None,
    critical: bool = False,
) -> None:
    """Record a governance enforcement action to the tamper-evident audit log.

    Emits via ``log_action`` directly (not the optional audit hook), so a
    governance event can't be silently disabled by hook configuration.

    ``critical`` is for enforcement decisions whose audit must not be lost even
    when the async audit queue is saturated: the rare *reject* paths (a write
    was refused with 4xx/5xx) pass ``critical=True`` so ``log_action`` falls
    back to a synchronous storage write on queue overflow instead of dropping
    the event. Higher-volume non-reject signals (flag / keep-private) stay
    best-effort and ride the queue's normal back-pressure (drop-with-counter
    under sustained overload) — the request hot path is never blocked for them.
    """
    await log_action(
        tenant_id=tenant_id,
        agent_id=agent_id,
        action=action,
        resource_type="memory",
        resource_id=resource_id,
        detail=detail,
        critical=critical,
    )
