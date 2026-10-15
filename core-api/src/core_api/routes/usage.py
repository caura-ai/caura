"""``GET /api/v1/spend``: a workspace's model calls and recall savings.

Named ``spend`` rather than ``usage``: the enterprise gateway already routes
``/api/v1/usage/`` to the plan-usage counters.

Reads the per-minute totals usage metering keeps (``common.usage``, migration
066) for one tenant over a window, grouped as asked, and adds the derived
figures a report shows: total tokens, and context saved (the full size of the
parents chunked results came from, less what was returned).

Who may read it: a tenant or admin credential. Spend per agent is an
admin-plane report, gated like tenant settings — an agent key cannot read its
peers' spend, and an org member is refused like on settings. Counts only;
nothing here is memory content.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client

router = APIRouter(tags=["Usage"])

_GROUPS = frozenset({"day", "agent_id", "fleet_id", "operation", "kind", "provider", "model", "metric"})
_MAX_WINDOW = timedelta(days=366)


def summarize(model_calls: list[dict], savings: list[dict]) -> dict:
    """The headline figures, over whatever the rows cover."""
    calls = sum(int(r.get("calls", 0)) for r in model_calls)
    tokens_in = sum(int(r.get("tokens_in", 0)) for r in model_calls)
    tokens_out = sum(int(r.get("tokens_out", 0)) for r in model_calls)
    reasoning = sum(int(r.get("tokens_reasoning", 0)) for r in model_calls)
    by_metric: dict[str, int] = {}
    for r in savings:
        by_metric[r["metric"]] = by_metric.get(r["metric"], 0) + int(r.get("quantity", 0))
    source = by_metric.get("recall.derived_source_tokens", 0)
    returned = by_metric.get("recall.derived_returned_tokens", 0)
    return {
        "model_calls": calls,
        "failed_attempts": sum(int(r.get("failures", 0)) for r in model_calls),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_reasoning": reasoning,
        "tokens_total": tokens_in + tokens_out + reasoning,
        "recalls": by_metric.get("recall.count", 0),
        "tokens_returned": by_metric.get("recall.returned_tokens", 0),
        # A chunked result saves the rest of its parent; a deleted parent was
        # never counted, so this cannot go negative in practice — floored anyway.
        "context_tokens_saved": max(0, source - returned),
        "cross_agent_results": by_metric.get("recall.cross_agent_results", 0),
        "duplicates_retired": by_metric.get("hygiene.retired_memories", 0),
        "duplicate_tokens_retired": by_metric.get("hygiene.retired_tokens", 0),
        "merge_tokens_written": by_metric.get("hygiene.written_tokens", 0),
    }


@router.get("/spend")
async def get_usage(
    tenant_id: str | None = Query(default=None),
    start: datetime | None = Query(default=None, description="Inclusive; default 30 days before `end`."),
    end: datetime | None = Query(default=None, description="Exclusive; default now."),
    group_by: str = Query(
        default="day",
        description="Comma-separated: day, agent_id, fleet_id, operation, kind, provider, model, metric.",
    ),
    auth: AuthContext = Depends(get_auth_context),
) -> dict:
    """Model calls (tokens in, out and reasoning) and recall savings for one workspace."""
    auth.enforce_not_agent_credential("read usage")
    auth.enforce_not_org_member("read usage")
    if tenant_id:
        auth.enforce_readable_tenant(tenant_id)
    elif auth.tenant_id:
        tenant_id = auth.tenant_id
    else:
        raise HTTPException(status_code=400, detail="tenant_id is required")

    end = (end or datetime.now(UTC)).astimezone(UTC)
    start = (start or end - timedelta(days=30)).astimezone(UTC)
    if end <= start:
        raise HTTPException(status_code=422, detail="'end' must be after 'start'")
    if end - start > _MAX_WINDOW:
        raise HTTPException(status_code=422, detail="the window may span at most 366 days")
    groups = [g.strip() for g in group_by.split(",") if g.strip()]
    unknown = sorted(set(groups) - _GROUPS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown group_by: {', '.join(unknown)}")

    totals = await get_storage_client().query_usage(
        tenant_id,
        {"start": start.isoformat(), "end": end.isoformat(), "group_by": list(dict.fromkeys(groups))},
    )
    model_calls = totals.get("model_calls") or []
    savings = totals.get("savings") or []
    return {
        "tenant_id": tenant_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "group_by": groups,
        "summary": summarize(model_calls, savings),
        "model_calls": model_calls,
        "savings": savings,
    }
