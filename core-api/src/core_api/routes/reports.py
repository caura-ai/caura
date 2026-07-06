"""Report API — ``GET /api/v1/reports``.

Daily/weekly governed activity report — "what each agent did" — backing the
report → product-page → agent-self-report flow (an agent fetches its own report
and surfaces it in its 1:1 with its owner, or in its group). MemClaw returns the
governed data; the agent's runtime does the messaging.

Two-check governed read (order matters):

1. **Authorization (authoritative, server-verified).** ``resolve_caller_and_gate``
   resolves the caller (gateway-verified ``X-Agent-ID`` > query > default) and
   gates on trust (scope=agent → trust ≥ 1). The data is then scoped to the
   caller's own tenant + own fleet (team/org-visible) + own agent rows — exactly
   what the caller can already recall. Cross-fleet / tenant-wide reporting is
   deliberately NOT exposed here (would need trust ≥ 2; future admin surface).

2. **Destination down-filter (client-asserted, NARROW-ONLY).** The agent declares
   the delivery *audience class* it will surface into. This can only ever
   *narrow* the result from check 1 — never widen it. Absent/unknown destination
   ⇒ fail closed to the most restrictive class.

The corpus is restricted to **durable, decision-bearing** memories: episodic
activity-log types and the unattributed ``main`` firehose agent are excluded so
the report reflects durable per-agent work rather than the raw activity stream.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.services.audit_service import log_action
from core_api.services.caller_identity import resolve_caller_and_gate

router = APIRouter(tags=["Reports"])

# Episodic activity-log type(s) + the unattributed firehose agent excluded so the
# report reflects durable, decision-bearing per-agent work.
NON_DURABLE_TYPES = ("episode",)
RESERVED_FIREHOSE_AGENTS = ("main",)

# "Cohesive" filter: heartbeat / health-check / status-poll noise leaks in as
# NON-episode rows (e.g. action/outcome "heartbeat" or "Checked HEARTBEAT.md"
# writes), so the type+firehose exclusion above is not enough — a per-agent
# leaderboard built on it still counts monitoring pings as "what the agent did".
# This case-insensitive title regex drops that noise across ALL types so the
# report reflects real work; genuine rules/decisions/insights never match it, so
# the value/quality surfaces are unaffected. Applied report-wide (breakdown,
# durable rows, trend) for a single reconcilable corpus. Pure-monitor agents
# fall off the leaderboard naturally once their pings are excluded.
NON_COHESIVE_TITLE_REGEX = (
    r"(heartbeat|health[- ]?check|healthz|healthy|watchdog|gpu.?health|no.?change|"
    r"auth error|zero auth|0 auth|encrypted|unreadable|no readable|no actionable|"
    r"no usable|no_reply|polled|quickcheck|app-fleet|discovery script|"
    r"gateway (active|reachable)|cache refresh)"
)
_NON_COHESIVE_TITLE_RE = re.compile(NON_COHESIVE_TITLE_REGEX, re.IGNORECASE)

# Delivery audience classes — an abstraction over messaging platforms
# (WhatsApp / Teams / Slack / Claude-Code map to one of these at the edge).
AUDIENCE_OWNER_1TO1 = "owner_1to1"
AUDIENCE_GROUP = "internal_group"
AUDIENCE_PRIVATE = "private_session"
AUDIENCE_EXTERNAL = "external"
_KNOWN_DESTINATIONS = {AUDIENCE_OWNER_1TO1, AUDIENCE_GROUP, AUDIENCE_PRIVATE, AUDIENCE_EXTERNAL}
# Audiences that may see per-agent detail + learning titles. ``external`` (and any
# unknown value, which we coerce to ``external``) is fail-closed to summary only.
_DETAIL_AUDIENCES = {AUDIENCE_OWNER_1TO1, AUDIENCE_GROUP, AUDIENCE_PRIVATE}
# "self" audiences scope to the caller's OWN contributions (narrowest).
_SELF_AUDIENCES = {AUDIENCE_OWNER_1TO1, AUDIENCE_PRIVATE}

_PERIOD_DAYS = {"day": 1, "week": 7}
_LEARNING_LIMIT = 5
_HIGHLIGHTS_LIMIT = 5
_TOP_AGENTS_LIMIT = 25
# PRE-FILTER fetch sizes, NOT final result sizes. The list API (see the warning
# at _cohesive() below) cannot push exclude_memory_types/exclude_agent_ids/
# exclude_title_regex server-side, so these rows are fetched raw and the noise
# (episode/firehose/heartbeat) is dropped client-side by _cohesive(). Both must
# stay large enough that the POST-_cohesive() pool still exceeds the downstream
# limits (_LEARNING_LIMIT=5, _HIGHLIGHTS_LIMIT=5) even in noisy corpora — a
# ~3-5x margin over the known exclusion rate.
#
# Recent in-window durable rows (created_at desc) feeding LEARNING (recent
# insights) and the working-on LANES (keyword categorization).
_DURABLE_FETCH_LIMIT = 600  # was 200
# Separate recall-sorted fetch backing VALUE HIGHLIGHTS + the spotlight headline,
# so "most-reused" is the true top-by-recall in the window — not merely the
# most-reused among the most-recent rows.
_HIGHLIGHTS_FETCH_LIMIT = 150  # was 40
# Activity-over-time trend length (daily buckets), independent of the period toggle.
_TREND_DAYS = 14
# "What the org is working on" lanes — heuristic keyword match on title/content,
# with a memory-type fallback so every durable memory lands in a lane.
_LANE_KEYWORDS = {
    "Governing": (
        "rule",
        "policy",
        "keystone",
        "governance",
        "complian",
        "trust",
        "permission",
        "access",
        "audit",
        "security",
        "regulat",
        "privacy",
        "gdpr",
        "pii",
        "insider",
    ),
    "Building": (
        "build",
        "ship",
        "feature",
        "implement",
        "deploy",
        "product",
        "integration",
        "pipeline",
        "engine",
        "migration",
        "release",
        "launch",
        "develop",
        "signal",
        "portfolio",
    ),
    "Operating": (
        "monitor",
        "incident",
        "alert",
        "outage",
        "health",
        "disk",
        "dba",
        "reliab",
        "restart",
        "latency",
        "downtime",
        "backup",
        "on-call",
        "stall",
    ),
}
_TYPE_LANE = {
    "rule": "Governing",
    "decision": "Governing",
    "preference": "Governing",
    "commitment": "Governing",
    "insight": "Governing",
    "plan": "Building",
    "task": "Building",
    "action": "Building",
    "intention": "Building",
    "semantic": "Building",
    "fact": "Building",
    "outcome": "Operating",
    "episode": "Operating",
    "cancellation": "Operating",
}


@router.get("/reports")
async def get_report(
    tenant_id: str = Query(..., description="Tenant scope (validated against the calling credential)."),
    period: str = Query("week", description="Reporting window: 'day' or 'week'."),
    destination: str = Query(
        AUDIENCE_EXTERNAL,
        description=(
            "Delivery audience class the agent will surface this into: "
            "'owner_1to1', 'internal_group', 'private_session', or 'external'. "
            "Narrows the result; never widens it. Unknown/absent ⇒ most restrictive."
        ),
    ),
    agent_id: str | None = Query(
        None, description="Caller agent id (gateway-verified X-Agent-ID wins when present)."
    ),
    scope: str = Query(
        "own",
        description=(
            "Data breadth: 'own' (home tenant, default) or 'org' (aggregate across "
            "every tenant the credential may read — requires a cross-tenant read key)."
        ),
    ),
    readable_tenant_ids: str | None = Query(
        None,
        description=(
            "CSV of tenant ids to aggregate under scope='org'. Honored ONLY for the "
            "internal admin credential (the org-report proxy); every other caller is "
            "pinned to its own credential's readable set and this value is ignored."
        ),
    ),
    auth: AuthContext = Depends(get_auth_context),
) -> dict:
    # Tenant scope is validated against the credential: agent creds can only
    # report on their own tenant; admin keys may target any tenant.
    auth.enforce_tenant(tenant_id)
    if period not in _PERIOD_DAYS:
        raise HTTPException(status_code=422, detail=f"Invalid period '{period}'. Use 'day' or 'week'.")
    if scope not in ("own", "org"):
        raise HTTPException(status_code=422, detail="Invalid scope. Use 'own' or 'org'.")

    # ── Org breadth ("true org level" = a credential capability, not a
    # memory scope). ``scope='org'`` aggregates across the caller's readable
    # tenant set and REQUIRES a cross-tenant read credential — a home-only key
    # cannot widen. Attribution still lives in each tenant's writes; the widened
    # breakdown keeps Inv-Scope (no agent_id ⇒ excludes ``scope_agent``). ──
    org_mode = scope == "org"
    if org_mode and not (auth.is_admin or auth.is_cross_tenant_read):
        raise HTTPException(
            status_code=403,
            detail="scope='org' requires a cross-tenant read credential.",
        )
    # The internal admin credential (the enterprise org-report proxy) may pass an
    # explicit tenant set — the proxy has already org-admin-gated the caller and
    # resolved the org's own tenants. Every other caller is pinned to its own
    # credential's readable set; a client-supplied value is ignored so a
    # cross-tenant agent cannot widen past its grant.
    if org_mode and auth.is_admin and readable_tenant_ids:
        readable: list[str] | None = [t.strip() for t in readable_tenant_ids.split(",") if t.strip()]
    else:
        readable = auth.readable_tenant_ids if org_mode else None

    # ── Check 1: authorization. ──
    # ``enforce_tenant`` (above) is the base authz: the caller is a member/admin
    # of this tenant. An AGENT caller (gateway-verified ``X-Agent-ID`` or an
    # explicit ``agent_id``) is additionally gated on trust ≥ 1 and resolved for
    # the self view. A human/tenant dashboard caller has no agent identity — it
    # gets the tenant GROUP view (never another agent's private rows; the
    # breakdown's own visibility scoping excludes ``scope_agent`` when no agent
    # is set). This avoids 403-ing a logged-in human on the unregistered default
    # agent id.
    asserted_agent = auth.agent_id or agent_id
    caller_agent_id: str | None = None
    if asserted_agent:
        caller_agent_id = await resolve_caller_and_gate(
            auth,
            tenant_id=tenant_id,
            body_agent_id=asserted_agent,
            scope="agent",
            action="reports",
        )

    # Caller's agent row → fleet (data scope) + belonging (audience target).
    sc = get_storage_client()
    caller = (await sc.get_agent(caller_agent_id, tenant_id) or {}) if caller_agent_id else {}
    caller_fleet = caller.get("fleet_id")
    belonging_type = caller.get("belonging_type") or "service"
    owner_ref = caller.get("owner_ref")

    # ── Check 2: destination → data scope (NARROW-ONLY; unknown ⇒ external). ──
    dest = destination if destination in _KNOWN_DESTINATIONS else AUDIENCE_EXTERNAL
    now = datetime.now(UTC)
    window_start = now - timedelta(days=_PERIOD_DAYS[period])

    breakdown_query: dict = {
        "tenant_id": tenant_id,
        "created_after": window_start.isoformat(),
        "exclude_memory_types": list(NON_DURABLE_TYPES),
        "exclude_agent_ids": list(RESERVED_FIREHOSE_AGENTS),
        "exclude_title_regex": NON_COHESIVE_TITLE_REGEX,
        # Count agent-private (scope_agent) durable writes in the AGGREGATES
        # (totals/by_type/by_agent/quality/trend). These are real, decision-
        # bearing memories an agent kept private — excluding them made
        # durable_memories_written measure "team-visible" rather than "written",
        # and disproportionately undercounted privacy-heavy tenants. This is a
        # pure count; the content sections (value_highlights/learning/spotlight/
        # working_on) still exclude scope_agent — see list_query below — so no
        # private content is surfaced to the group. Ignored on the self path
        # (agent_id set), which already scopes visibility to the caller.
        "include_scope_agent": True,
    }
    if org_mode:
        # Org-wide: aggregate across every tenant the credential may read.
        # Team/org-visible only (no agent_id ⇒ excludes scope_agent); the
        # breakdown returns a per-tenant ``by_tenant`` when the set spans >1.
        breakdown_query["readable_tenant_ids"] = readable
        scope_label = "org"
    elif dest in _SELF_AUDIENCES and caller_agent_id:
        # Narrowest: only the caller's own contributions.
        breakdown_query["agent_id"] = caller_agent_id
        scope_label = "self"
    else:
        # internal_group / external: the caller's own fleet (team/org-visible).
        # ``external`` shares the same query but its detail is stripped below.
        if caller_fleet:
            breakdown_query["fleet_id"] = caller_fleet
        scope_label = "group"

    breakdown = await sc.memory_stats_breakdown(breakdown_query)
    by_agent = {
        a: c for a, c in (breakdown.get("by_agent") or {}).items() if a not in RESERVED_FIREHOSE_AGENTS
    }
    by_type = breakdown.get("by_type") or {}
    durable_total = int(breakdown.get("total", 0) or 0)
    by_tenant = breakdown.get("by_tenant") or {}  # populated only in org scope (>1 tenant)

    per_agent: list[dict] = []
    learning: list[dict] = []
    value_highlights: list[dict] = []
    spotlight: dict | None = None
    trend: list[dict] = []
    working_on: dict = {}
    quality: dict = {}
    if dest in _DETAIL_AUDIENCES or org_mode:

        def _title(m: dict) -> str:
            return m.get("title") or (m.get("metadata") or {}).get("summary") or "(untitled)"

        def _rank(m: dict) -> tuple:
            return (m.get("recall_count") or 0, m.get("created_at") or "")

        def _cohesive(m: dict) -> bool:
            # WARNING: the /memories/list API cannot push exclude_memory_types /
            # exclude_agent_ids / exclude_title_regex server-side, so the row
            # fetches (list_query, highlights_query) come back raw and this
            # predicate drops the noise client-side — AFTER the fetch limit has
            # already been consumed. That is why _DURABLE_FETCH_LIMIT /
            # _HIGHLIGHTS_FETCH_LIMIT over-fetch: the surviving pool must still
            # exceed the downstream _LEARNING_LIMIT / _HIGHLIGHTS_LIMIT slices.
            #
            # Mirror the server-side report corpus in Python for the row fetches:
            # durable (non-episodic, non-firehose) AND not heartbeat/status noise.
            # Title-only match mirrors the ``exclude_title_regex`` predicate.
            return (
                m.get("memory_type") not in NON_DURABLE_TYPES
                and m.get("agent_id") not in RESERVED_FIREHOSE_AGENTS
                and not _NON_COHESIVE_TITLE_RE.search(m.get("title") or "")
            )

        # Recent-ordered fetch → LEARNING (recent insights) + working-on LANES.
        list_query: dict = {
            "tenant_id": tenant_id,
            "caller_agent_id": caller_agent_id,
            "created_after": window_start.isoformat(),
            "sort": "created_at",
            "order": "desc",
            "limit": _DURABLE_FETCH_LIMIT,
        }
        # Self audiences scope to the caller's OWN authored rows, mirroring the
        # ``agent_id`` filter applied to breakdown_query on this path — so the
        # list-derived sections (learning, value_highlights, working_on) stay
        # consistent with the breakdown-derived counts (durable_total, per_agent).
        # NOTE: /memories/list filters authorship via ``written_by`` (``agent_id``
        # is not read on that path); ``caller_agent_id`` above is the visibility
        # identity, not an author filter. Skipped in org_mode: an org-scope report
        # aggregates the whole readable set, so it must not narrow to one author.
        if dest in _SELF_AUDIENCES and caller_agent_id and not org_mode:
            list_query["written_by"] = caller_agent_id
        if org_mode:
            list_query["readable_tenant_ids"] = readable
        elif dest == AUDIENCE_GROUP and caller_fleet:
            list_query["fleet_id"] = caller_fleet
        # Recall-sorted fetch → VALUE HIGHLIGHTS + spotlight headline (true
        # top-by-recall, not merely the most-reused among the most-recent rows).
        # Derived from list_query AFTER the written_by/fleet/readable conditionals.
        highlights_query = dict(list_query, sort="recall_count", limit=_HIGHLIGHTS_FETCH_LIMIT)

        # ── Phase 1: independent storage reads, concurrently. The agent
        # leaderboard (group only) and the trend fetch (group/org only) are
        # conditional. ──
        want_trend = dest == AUDIENCE_GROUP or org_mode
        want_agents = dest == AUDIENCE_GROUP and not org_mode
        phase1: dict[str, Awaitable[Any]] = {
            "recent": sc.list_memories_by_filters(list_query),
            "recall": sc.list_memories_by_filters(highlights_query),
        }
        if want_agents:
            phase1["agents"] = sc.list_agents(tenant_id, caller_fleet)
        if want_trend:
            # Activity-over-time trend (group/org only). Built here — inside the
            # guard — so trend_query is a concrete dict, not dict | None.
            trend_query: dict = {
                "tenant_id": tenant_id,
                "since": (now - timedelta(days=_TREND_DAYS)).isoformat(),
                "exclude_memory_types": list(NON_DURABLE_TYPES),
                "exclude_agent_ids": list(RESERVED_FIREHOSE_AGENTS),
                "exclude_title_regex": NON_COHESIVE_TITLE_REGEX,
                # Match the breakdown: the trend must count private durable rows
                # too, or the daily line won't sum to durable_memories_written.
                "include_scope_agent": True,
            }
            if org_mode:
                trend_query["readable_tenant_ids"] = readable
            elif caller_fleet:
                trend_query["fleet_id"] = caller_fleet
            phase1["trend"] = sc.memory_daily_durable_counts(trend_query)
        p1_keys = list(phase1)
        p1_vals = await asyncio.gather(*(phase1[k] for k in p1_keys))
        p1 = dict(zip(p1_keys, p1_vals))

        durable = [m for m in (p1["recent"] or []) if _cohesive(m)]
        top_durable = [m for m in (p1["recall"] or []) if _cohesive(m)]

        # Per-agent leaderboard, joined with belonging metadata. Group = the whole
        # fleet (list_agents); self = just the caller's own row (already fetched);
        # org scope skips the join (list_agents is per-tenant, can't cover the
        # readable set — cross-tenant agents get null).
        belong_by_id: dict[str, dict] = {}
        if not org_mode:
            if dest == AUDIENCE_GROUP:
                agent_rows = p1.get("agents") or []
            elif dest in _SELF_AUDIENCES:
                agent_rows = [caller] if caller else []
            else:
                agent_rows = []
            for row in agent_rows:
                aid = row.get("agent_id")
                if aid:
                    belong_by_id[aid] = row
        for aid, cnt in sorted(by_agent.items(), key=lambda kv: kv[1], reverse=True)[:_TOP_AGENTS_LIMIT]:
            row = belong_by_id.get(aid, {})
            per_agent.append(
                {
                    "agent_id": aid,
                    "display_name": row.get("display_name"),
                    "belonging_type": row.get("belonging_type"),
                    "durable_writes": cnt,
                }
            )

        # Learning: most recent distilled insights in the window.
        learning = [
            {"title": _title(m), "created_at": m.get("created_at")}
            for m in durable
            if m.get("memory_type") == "insight"
        ][:_LEARNING_LIMIT]

        # Value highlights: the most-reused (all-time) durable knowledge authored
        # in-window. NOTE: recall_count is a lifetime counter — MemClaw has no
        # per-period recall log — so this is not "reused *this* period".
        value_highlights = [
            {
                "title": _title(m),
                "type": m.get("memory_type"),
                "recall_count": m.get("recall_count") or 0,
                "agent_id": m.get("agent_id"),
            }
            for m in top_durable[:_HIGHLIGHTS_LIMIT]
        ]

        # Spotlight: the top contributor + their headline (highest-recall) memory.
        if per_agent:
            top = per_agent[0]
            # Search a deduped union of both fetches so the headline is the top
            # agent's highest-recall in-window memory, whether it surfaced via the
            # recall-sorted or the recent-ordered fetch.
            pool = list({m.get("id"): m for m in (*top_durable, *durable)}.values())
            authored = sorted(
                (m for m in pool if m.get("agent_id") == top["agent_id"]),
                key=_rank,
                reverse=True,
            )
            spotlight = {
                "agent_id": top["agent_id"],
                "durable_writes": top["durable_writes"],
                "headline": (
                    {"title": _title(authored[0]), "type": authored[0].get("memory_type")}
                    if authored
                    else None
                ),
            }

        # Working-on lanes: heuristic categorization of the window's durable work
        # (keyword match on title/content, else a memory-type fallback).
        lanes: dict[str, dict] = {
            name: {"count": 0, "items": []} for name in ("Governing", "Building", "Operating")
        }
        for m in durable:
            text = (_title(m) + " " + (m.get("content") or "")).lower()
            lane = next(
                (name for name, kws in _LANE_KEYWORDS.items() if any(k in text for k in kws)),
                None,
            )
            if lane is None:
                lane = _TYPE_LANE.get(m.get("memory_type") or "", "Building")
            lanes[lane]["count"] += 1
            if len(lanes[lane]["items"]) < 3:
                lanes[lane]["items"].append(_title(m))
        working_on = lanes

        # Activity-over-time trend (group/org view): assembled from the Phase-1
        # daily-durable-counts fetch, bucketed into the last _TREND_DAYS days.
        if want_trend:
            raw_counts = {r["day"]: r["count"] for r in (p1.get("trend") or [])}
            trend = [
                {
                    "day": (now.date() - timedelta(days=d)).isoformat(),
                    "count": raw_counts.get((now.date() - timedelta(days=d)).isoformat(), 0),
                }
                for d in reversed(range(_TREND_DAYS))
            ]

        # ── Quality: how good is the durable corpus (not just how much). ──
        # reuse-rate by type, never-recalled %, recall concentration (top-6
        # share), insight freshness, and the write→durable→reused funnel.
        # Scope keys shared by the three (independent) quality calls below.
        # ``include_scope_agent`` rides along so the funnel's "written" (full) and
        # the insight-freshness corpus (ins) count private rows too — otherwise
        # ``durable`` (which now includes them) could exceed ``written`` and break
        # the write→durable→reused funnel invariant.
        scope_keys = {
            k: breakdown_query[k]
            for k in ("tenant_id", "agent_id", "fleet_id", "readable_tenant_ids", "include_scope_agent")
            if k in breakdown_query
        }
        # ── Phase 2: quality metrics + the two supporting breakdown calls run
        # concurrently — all independent, and derived only from breakdown_query
        # (already resolved), not from the main breakdown's results.
        #   - memory_quality_metrics: reuse-rate/never/concentration over the corpus
        #   - insight breakdown: lifetime insight corpus by status (freshness)
        #   - funnel breakdown: full-corpus writes in the window (no excludes)
        qm, ins, full = await asyncio.gather(
            sc.memory_quality_metrics(breakdown_query),
            sc.memory_stats_breakdown({**scope_keys, "memory_type": "insight"}),
            sc.memory_stats_breakdown({**scope_keys, "created_after": window_start.isoformat()}),
        )
        q_total = int(qm.get("total", 0) or 0)
        q_reused = int(qm.get("reused", 0) or 0)
        q_recalls = int(qm.get("total_recalls", 0) or 0)
        top6 = sum(qm.get("top_recalls") or [])
        reuse_by_type = sorted(
            (
                {
                    "type": t,
                    "total": int(v.get("total", 0) or 0),
                    "reused": int(v.get("reused", 0) or 0),
                    "reuse_pct": round(100.0 * v["reused"] / v["total"], 1) if v.get("total") else 0.0,
                }
                for t, v in (qm.get("by_type") or {}).items()
            ),
            key=lambda r: r["reuse_pct"],
            reverse=True,
        )
        # Insight freshness — "outdated"/"archived" = gone stale.
        ins_status = ins.get("by_status") or {}
        ins_total = int(ins.get("total", 0) or 0)
        ins_stale = int(ins_status.get("outdated", 0) or 0) + int(ins_status.get("archived", 0) or 0)
        written = int(full.get("total", 0) or 0)
        quality = {
            "never_recalled_pct": round(100.0 * (q_total - q_reused) / q_total, 1) if q_total else 0.0,
            "recall_concentration_pct": round(100.0 * top6 / q_recalls, 1) if q_recalls else 0.0,
            "recall_concentration_top6": top6,
            "total_recalls": q_recalls,
            "reuse_by_type": reuse_by_type,
            "insight_freshness": {
                "total": ins_total,
                "stale": ins_stale,
                "stale_pct": round(100.0 * ins_stale / ins_total, 1) if ins_total else 0.0,
                "by_status": ins_status,
            },
            "funnel": {"written": written, "durable": q_total, "reused": q_reused},
        }

    # ── Audit the read + the declared destination (provenance / no-leakage trail). ──
    await log_action(
        tenant_id=tenant_id,
        action="report_read",
        resource_type="report",
        detail={
            "period": period,
            "destination": dest,
            "scope": scope_label,
            "agent_id": caller_agent_id,
        },
    )

    return {
        "meta": {
            "period": period,
            "window_start": window_start.isoformat(),
            "window_end": now.isoformat(),
            "tenant_id": tenant_id,
            "fleet_id": None if org_mode else caller_fleet,
            "tenants": len(readable) if org_mode and readable else 1,
            "destination": dest,
            "scope": scope_label,
            "corpus": (
                "durable, decision-bearing memories (excl. episodic logs, the "
                "'main' firehose, and heartbeat/health/status noise)"
            ),
            "belonging": (
                {"type": belonging_type, "owner_ref": owner_ref}
                if dest in _SELF_AUDIENCES and not org_mode
                else None
            ),
        },
        "summary": {
            "durable_memories_written": durable_total,
            "active_agents": len(by_agent),
            "by_type": by_type,
            "by_tenant": by_tenant,
        },
        "per_agent": per_agent,
        "value_highlights": value_highlights,
        "spotlight": spotlight,
        "trend": trend,
        "working_on": working_on,
        "learning": learning,
        "quality": quality,
    }
