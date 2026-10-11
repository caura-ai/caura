"""``GET /api/v1/value``: what a workspace's memory did for its agents.

docs/plans/value-metering.md. The figures, and the rules that keep them honest:

* ``sessions`` — agent sessions active in the window; ``sessions_helped`` —
  those that recalled at least one memory written in another session whose
  origin was recorded. A memory older than value metering is still counted as
  reused, but not as help: the work behind it is unknown.
* ``work_reused`` — the work behind the memories those sessions reused, each
  memory counted once per session: minutes (always observed), calls to Caura
  in that stretch (a lower bound on turns), and tokens/turns where a hook,
  plugin or SDK reported them, with how many reuses that covers. An upper
  bound on the work saved: a recall does not always mean the work would have
  been redone in full.
* ``knowledge_shared`` — reuses where the reading agent is not the writer,
  and the agents whose memories others use most ("teachers") and who use
  others' most ("learners").
* ``prevented`` — contradictions a person resolved (dismissed ones were
  false alarms) and outdated memories kept out of recall, in the window; and
  the contradictions still waiting for review, which are not prevented yet.
* ``caura_cost`` — Caura's own model tokens, one line (usage metering).

Gated like ``/spend``: tenant and admin credentials; agent keys and org
members get 403.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query

from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client

router = APIRouter(tags=["Usage"])

_MAX_WINDOW = timedelta(days=366)


def shape(raw: dict, spend_summary: dict | None) -> dict:
    """Turn storage's figures into the report's sections."""
    t = raw.get("totals") or {}
    sessions = int(raw.get("sessions") or 0)
    helped = int(t.get("sessions_helped") or 0)
    reuses = int(t.get("reuses") or 0)
    return {
        "sessions": sessions,
        "sessions_helped": helped,
        "sessions_helped_share": (helped / sessions) if sessions else None,
        "work_reused": {
            "reuses": reuses,
            "reuses_with_origin": int(t.get("reuses_with_origin") or 0),
            "memories_reused": int(t.get("memories_reused") or 0),
            "minutes": round(int(t.get("origin_seconds") or 0) / 60, 1),
            "reuses_with_time": int(t.get("reuses_with_time") or 0),
            "calls_lower_bound": int(t.get("origin_calls") or 0),
            "tokens_reported": int(t.get("origin_tokens_in") or 0) + int(t.get("origin_tokens_out") or 0),
            "turns_reported": int(t.get("origin_turns") or 0),
            "reuses_with_tokens": int(t.get("reuses_with_tokens") or 0),
        },
        "knowledge_shared": {
            "cross_agent_reuses": int(t.get("cross_agent_reuses") or 0),
            "cross_agent_share": (int(t.get("cross_agent_reuses") or 0) / reuses) if reuses else None,
            "teachers": raw.get("teachers") or [],
            "learners": raw.get("learners") or [],
        },
        "prevented": raw.get("prevented") or {},
        "daily": raw.get("daily") or [],
        "caura_cost": (
            {
                "model_tokens": int(spend_summary.get("tokens_total") or 0),
                "model_calls": int(spend_summary.get("model_calls") or 0),
            }
            if spend_summary
            else None
        ),
    }


@router.get("/value")
async def get_value(
    tenant_id: str | None = Query(default=None),
    start: datetime | None = Query(default=None, description="Inclusive; default 30 days before `end`."),
    end: datetime | None = Query(default=None, description="Exclusive; default now."),
    auth: AuthContext = Depends(get_auth_context),
) -> dict:
    """Sessions helped, work reused, knowledge shared, mistakes prevented, and Caura's cost."""
    auth.enforce_not_agent_credential("read value")
    auth.enforce_not_org_member("read value")
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

    sc = get_storage_client()
    window = {"start": start.isoformat(), "end": end.isoformat()}
    raw = await sc.value_query(tenant_id, window)
    try:
        spend = (await sc.query_usage(tenant_id, {**window, "group_by": []})).get("model_calls") or []
        spend_summary = {
            "tokens_total": sum(
                int(r.get("tokens_in", 0)) + int(r.get("tokens_out", 0)) + int(r.get("tokens_reasoning", 0))
                for r in spend
            ),
            "model_calls": sum(int(r.get("calls", 0)) for r in spend),
        }
    except Exception:
        spend_summary = None
    return {
        "tenant_id": tenant_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        **shape(raw, spend_summary),
    }
