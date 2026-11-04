"""``GET /api/v1/reports/evidence-coverage``: the evidence contract's two numbers.

docs/plans/evidence-contract-v1.md, "Done when". Per tenant:

* ``provenance`` — the share of rows that carry an agent (not a placeholder
  such as ``mcp-agent``, see ``PLACEHOLDER_AGENT_IDS``), a written-at time and
  a server-stamped ``trace_id``. Target 95%.
* ``external_evidence`` — the share that carry a ``tool_call_id`` or a real
  ``source_uri`` (not ingest's ``text-input`` placeholder nor a benchmark
  scheme, see ``NON_EVIDENCE_SOURCE_URIS``). Target 30%, stretch 50%.

The numbers are measured over the memories recall RETURNED in the window
(``recall_candidate.returned``, one per return, so a memory recalled ten times
counts ten times), which needs recall logging. When the window has no logged
returns they fall back to the tenant's live memories, and ``denominator`` says
which one was used. Both sets of counts are always in the body, along with the
memories written in the window: rows written before migration 071 carry no
``trace_id`` and are never backfilled, so the all-time memory figure trails the
recent one.

Gated like ``/value``: tenant and admin credentials; agent keys and org
members get 403. Counts only, no memory content.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from common.constants import NON_EVIDENCE_SOURCE_URIS, PLACEHOLDER_AGENT_IDS
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.services.organization_settings import resolve_config

router = APIRouter(tags=["Usage"])

PROVENANCE_TARGET = 0.95
EXTERNAL_EVIDENCE_TARGET = 0.30
EXTERNAL_EVIDENCE_STRETCH = 0.50


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _figures(counts: dict) -> dict:
    """One denominator's counts, with the two shares."""
    rows = int(counts.get("rows") or 0)
    return {
        **{k: int(v or 0) for k, v in counts.items()},
        "rows": rows,
        "provenance_share": _share(int(counts.get("provenance") or 0), rows),
        "external_evidence_share": _share(int(counts.get("external_evidence") or 0), rows),
    }


def shape(raw: dict, *, recall_logging: dict) -> dict:
    """Turn storage's counts into the report: headline numbers first, then every denominator."""
    recall = _figures(raw.get("recall") or {})
    memories = _figures(raw.get("memories") or {})
    if recall["rows"]:
        denominator, basis = "recall_candidates", recall
    else:
        denominator, basis = "memories", memories
    provenance = basis["provenance_share"]
    external = basis["external_evidence_share"]
    return {
        "denominator": denominator,
        "provenance": {
            "share": provenance,
            "target": PROVENANCE_TARGET,
            "meets_target": None if provenance is None else provenance >= PROVENANCE_TARGET,
        },
        "external_evidence": {
            "share": external,
            "target": EXTERNAL_EVIDENCE_TARGET,
            "stretch": EXTERNAL_EVIDENCE_STRETCH,
            "meets_target": None if external is None else external >= EXTERNAL_EVIDENCE_TARGET,
        },
        "recall_logging": recall_logging,
        "recall_candidates": recall,
        "memories": memories,
        "memories_written_in_window": _figures(raw.get("memories_in_window") or {}),
        "verification_status": raw.get("verification") or {},
        "rules": {
            "placeholder_agent_ids": list(PLACEHOLDER_AGENT_IDS),
            "non_evidence_source_uris": list(NON_EVIDENCE_SOURCE_URIS),
        },
    }


@router.get("/reports/evidence-coverage")
async def get_evidence_coverage(
    tenant_id: str | None = Query(default=None),
    window_days: int = Query(default=30, ge=1, le=366),
    auth: AuthContext = Depends(get_auth_context),
) -> dict:
    """Provenance and external-evidence coverage of what recall returned, per tenant."""
    auth.enforce_not_agent_credential("read evidence coverage")
    auth.enforce_not_org_member("read evidence coverage")
    if tenant_id:
        auth.enforce_readable_tenant(tenant_id)
    elif auth.tenant_id:
        tenant_id = auth.tenant_id
    else:
        raise HTTPException(status_code=400, detail="tenant_id is required")
    end = datetime.now(UTC)
    start = end - timedelta(days=window_days)
    raw = await get_storage_client().evidence_coverage(
        tenant_id, {"start": start.isoformat(), "end": end.isoformat()}
    )
    cfg = await resolve_config(tenant_id)
    return {
        "tenant_id": tenant_id,
        "window_days": window_days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        **shape(
            raw,
            recall_logging={
                "recall_logging_enabled": cfg.recall_logging_enabled,
                "search_recall_logging_enabled": cfg.search_recall_logging_enabled,
            },
        ),
    }
