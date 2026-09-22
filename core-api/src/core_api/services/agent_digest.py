"""Nightly per-agent activity digest generation (Phase 2b).

Precomputes the prose "what did this agent do this day/week" summaries that
``GET /api/v1/reports/agent-activity`` serves read-only. Runs OFF the request
path — a core-operations cron POSTs the admin fanout endpoint (see
``routes/reports.py``), which calls :func:`run_agent_digest`. An LLM pass per
agent is too slow/costly to run on demand, so results are cached in
``agent_activity_digests``.

v1 fans out INLINE with bounded concurrency (fine at on-prem/eToro tenant
counts); a per-tenant Pub/Sub fanout is the scale path if org counts grow.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from common.llm import call_with_fallback
from core_api.clients.storage_client import get_storage_client
from core_api.services.organization_settings import get_settings_for_display
from core_api.services.report_corpus import (
    NON_COHESIVE_TITLE_REGEX,
    is_cohesive,
    passes_noise_filter,
    resolve_parent,
)
from core_api.services.report_corpus import (
    PERIOD_DAYS as _PERIOD_DAYS,
)

logger = logging.getLogger(__name__)

# Bounded concurrency: cheap per-agent memory fetches vs. the expensive LLM pass.
_FETCH_CONCURRENCY = 8
_LLM_CONCURRENCY = 4
_ORG_CONCURRENCY = 4
# Pre-filter fetch size (cohesive filter runs client-side after the fetch).
_FETCH_LIMIT = 400
# Rough gpt-5.4-mini cost per digest call (~2k in + ~400 out). Only used to turn
# ``max_cost_per_run_usd`` into a call budget — an estimate, not billing-grade.
_PER_CALL_COST_USD = 0.005
# Min non-noise EPISODE events to summarize an agent that has no durable rows
# (its work IS episodic). Below this it still appears via the count-only tail.
_EVENT_FLOOR_DEFAULT = 1
# Cap on count-only ("listed") family rows past top_n, so an active-but-not-
# summarized agent is never invisible without flooding the report.
_LISTED_MAX_DEFAULT = 25
# Cap on the stored per-family subagent list (bounds row size; UI shows top-N).
_MAX_SUBAGENTS_STORED = 30

# JSON schema the model must return.
DIGEST_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "narrative": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "shipped": {"type": "array", "items": {"type": "string"}},
        "learned": {"type": "array", "items": {"type": "string"}},
        "open_threads": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["narrative"],
}

_SECTION_KEYS = ("decisions", "shipped", "learned", "open_threads")


def _build_prompt(agent: dict, mems: list[dict], period: str, mode: str = "durable") -> str:
    name = agent.get("display_name") or agent.get("agent_id")
    lines = []
    parent_id = agent.get("agent_id")
    for m in mems:
        title = m.get("title") or (m.get("metadata") or {}).get("summary") or "(untitled)"
        # In a rolled-up family, tag rows written by a subagent so the model can
        # attribute the work to the right child.
        src = m.get("agent_id", "")
        agent_tag = f" [from {src}]" if src and src != parent_id else ""
        lines.append(
            f"- [{m.get('memory_type', '?')}] {title}{agent_tag}"
            f" (recalled {m.get('recall_count') or 0}x, {str(m.get('created_at'))[:10]})"
        )
    corpus = "\n".join(lines)
    period_label = "day" if period == "day" else "week"
    # "activity" mode: the agent has no durable decisions/facts — its work is its
    # event log. Summarize the events as concrete activity rather than decisions.
    source_desc = "durable memories" if mode == "durable" else "activity-log events"
    return (
        f"You are writing a factual activity digest for the AI agent '{name}' over "
        f"the past {period_label}, grounded ONLY in its {source_desc} below.\n\n"
        f"Write a 2-4 sentence narrative of the concrete work the agent did — "
        f"decisions made, work shipped, things learned, open threads — quantifying "
        f"where natural (e.g. 'made 3 decisions'). Do not invent anything that is "
        f"not grounded in the memories.\n\n"
        f"IMPORTANT:\n"
        f"- Never comment on or critique the amount, richness, or quality of the "
        f"memories. Do not say they are thin, sparse, limited, or untitled, and do "
        f"not describe what is missing or what cannot be reconstructed.\n"
        f'- If the memories show no substantive work, set "narrative" to EXACTLY '
        f'"No significant work by {name} has been recorded during this period." '
        f"and leave all section lists empty.\n\n"
        f"Memories ({len(mems)}):\n{corpus}\n\n"
        f"Return JSON: narrative, decisions, shipped, learned, open_threads "
        f"(short bullet strings; empty if none), and confidence (0-1)."
    )


async def _summarize_agent(
    *,
    org_id: str,
    agent: dict,
    mems: list[dict],
    period: str,
    run_id: str,
    window_start: datetime,
    window_end: datetime,
    model: str,
    provider: str,
    truncated: bool,
    mode: str = "durable",
    subagents: list[dict] | None = None,
    source_count: int | None = None,
) -> str:
    """Summarize one agent family via the LLM and persist a row ONLY when we got
    a real narrative. Returns a status:

      ok / truncated — durable-corpus summary (row written)
      activity       — episode/event-log summary (agent had no durable rows)
      skipped        — LLM unavailable or returned no narrative; logged, NO row
      errored        — the LLM call raised; logged, NO row

    We deliberately do NOT persist a generic placeholder ("<agent> recorded N
    durable memories") when the model is unavailable — a template row is noise in
    the report, so the agent is dropped and the gap is logged instead.

    ``source_count`` is the family's total meaningful writes (the bar value);
    ``subagents`` is the rolled-up child list stored for the collapsed UI.
    """
    agent_id = agent.get("agent_id")
    prompt = _build_prompt(agent, mems, period, mode=mode)

    async def _call(llm: Any) -> dict:
        return await llm.complete_json(prompt, response_schema=DIGEST_SCHEMA)

    # call_with_fallback silently drops to _fake() when the real (and fallback)
    # provider both fail. This flag lets us detect that and skip rather than
    # persist a template narrative.
    used_fallback = False

    def _fake() -> dict:
        nonlocal used_fallback
        used_fallback = True
        return {}

    try:
        raw = await call_with_fallback(
            provider,
            _call,
            _fake,
            tenant_config=None,
            service_label="agent-digest",
            model_override=model,
        )
    except Exception as exc:
        logger.warning("agent_digest: LLM call failed for %s/%s: %r — skipping", org_id, agent_id, exc)
        return "errored"

    if used_fallback:
        logger.warning(
            "agent_digest: LLM unavailable for %s/%s — skipping (no row written)", org_id, agent_id
        )
        return "skipped"

    narrative = (raw.get("narrative") or "").strip() or None
    if not narrative:
        logger.warning("agent_digest: LLM returned no narrative for %s/%s — skipping", org_id, agent_id)
        return "skipped"

    sections = {k: raw.get(k) or [] for k in _SECTION_KEYS}
    # activity-mode (event-log) summaries are tagged distinctly; truncation only
    # relabels a durable summary.
    status = "activity" if mode == "activity" else ("truncated" if truncated else "ok")
    row = {
        "run_id": run_id,
        "tenant_id": org_id,
        "fleet_id": agent.get("fleet_id"),
        "agent_id": agent_id,
        "period": period,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "narrative": narrative,
        "sections": sections,
        "subagents": subagents or [],
        "source_count": source_count if source_count is not None else len(mems),
        "recall_count": sum(m.get("recall_count") or 0 for m in mems),
        "model": model,
        "status": status,
        "error_detail": None,
    }
    try:
        await get_storage_client().upsert_agent_activity_digest(row)
    except Exception as exc:
        # Re-raise so gather still counts this agent as errored; log for traceability.
        logger.warning("agent_digest: upsert failed for %s/%s: %r", org_id, agent_id, exc)
        raise
    return status


async def generate_for_org(
    org_id: str, period: str, config: dict, *, now: datetime, run_id: str | None = None
) -> dict:
    """Generate + persist digests for one org's most-active agents in the window.

    Fetches each agent's cohesive durable memories, ranks by volume, and
    LLM-summarizes the top ``top_n`` above ``min_activity_threshold``. Returns a
    per-org counts summary.
    """
    sc = get_storage_client()
    run_id = run_id or str(uuid.uuid4())
    days = _PERIOD_DAYS.get(period, 1)
    # Normalize to clean UTC boundaries so a run covers the full previous
    # day/week and re-runs on the same date reproduce the same window. Retention
    # (below) stays wall-clock on the raw ``now``.
    if period == "week":
        window_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
        window_end -= timedelta(days=window_end.weekday())  # Monday 00:00 UTC
    else:
        window_end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = window_end - timedelta(days=days)
    top_n = int(config.get("top_n") or 25)
    max_mems = int(config.get("max_memories_per_agent") or 60)
    # Durable floor: 1 by default — a single decision/fact is worth surfacing;
    # configurable to cut noise. Applies to day and week alike now.
    min_activity = int(config.get("min_activity_threshold") or 1)
    # Episode-fallback floor: min non-noise events to summarize an agent with no
    # durable rows (its work IS episodic). Below it the agent still lists.
    event_floor = int(config.get("event_floor") or _EVENT_FLOOR_DEFAULT)
    listed_max = int(config.get("listed_max") or _LISTED_MAX_DEFAULT)
    model = config.get("model") or "gpt-5.4-mini"
    provider = config.get("provider") or "openai"
    max_cost = float(config.get("max_cost_per_run_usd") or 0)

    agents = await sc.list_agents(org_id)
    known_ids = frozenset(a["agent_id"] for a in agents if a.get("agent_id"))

    # Fetch each agent's window memories (cheap; bounded concurrency). We already
    # pull ALL rows, so split them in-memory: durable (decision-bearing) vs.
    # non-noise events (episodes) — the latter is the fallback corpus for agents
    # whose real work is logged as episodes.
    fetch_sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _fetch(agent: dict) -> tuple[dict, list[dict], list[dict]]:
        async with fetch_sem:
            rows = await sc.list_memories_by_filters(
                {
                    "tenant_id": org_id,
                    "written_by": agent["agent_id"],
                    "created_after": window_start.isoformat(),
                    "created_before": window_end.isoformat(),
                    "sort": "created_at",
                    "order": "desc",
                    "limit": _FETCH_LIMIT,
                }
            )
            rows = rows or []
            durable = [m for m in rows if is_cohesive(m)]
            events = [m for m in rows if passes_noise_filter(m) and not is_cohesive(m)]
            return agent, durable, events

    fetched = await asyncio.gather(*(_fetch(a) for a in agents), return_exceptions=True)

    # Roll subagents up under their resolved parent (family) so a thin subagent
    # isn't invisible and dozens of them don't flood top_n. No extra query —
    # grouping + resolution are in-memory over the already-fetched rows.
    families: dict[str, dict] = {}
    for res in fetched:
        if isinstance(res, BaseException):
            logger.warning("agent_digest: fetch failed for an agent in %s: %r", org_id, res)
            continue
        agent, durable, events = res
        aid = agent["agent_id"]
        parent = resolve_parent(aid, known_ids)
        fam = families.setdefault(parent, {"durable": [], "events": [], "members": {}, "parent_agent": None})
        fam["durable"].extend(durable)
        fam["events"].extend(events)
        fam["members"][aid] = {
            "agent_id": aid,
            "fleet_id": agent.get("fleet_id"),
            "source_count": len(durable) + len(events),
        }
        if aid == parent:
            fam["parent_agent"] = agent

    def _family_agent(parent: str, fam: dict) -> dict:
        if fam["parent_agent"]:
            return fam["parent_agent"]
        # Only subagents exist (no standalone parent row) — synthesize one,
        # borrowing the busiest member's fleet.
        busiest = max(fam["members"].values(), key=lambda m: m["source_count"], default=None)
        return {"agent_id": parent, "fleet_id": busiest["fleet_id"] if busiest else None}

    def _subagents(parent: str, fam: dict) -> list[dict]:
        subs = [m for aid, m in fam["members"].items() if aid != parent and m["source_count"] > 0]
        subs.sort(key=lambda m: m["source_count"], reverse=True)
        return subs[:_MAX_SUBAGENTS_STORED]

    # Active families, ranked by total meaningful writes.
    active = [
        {
            "parent": p,
            "agent": _family_agent(p, fam),
            "durable": fam["durable"],
            "events": fam["events"],
            "total": len(fam["durable"]) + len(fam["events"]),
            "subagents": _subagents(p, fam),
        }
        for p, fam in families.items()
        if (len(fam["durable"]) + len(fam["events"])) > 0
    ]
    active.sort(key=lambda f: f["total"], reverse=True)

    def _corpus(f: dict) -> tuple[list[dict], str] | None:
        """Durable-first; else the episode/event fallback (ranked by recall)."""
        if len(f["durable"]) >= min_activity:
            return f["durable"], "durable"
        if len(f["events"]) >= event_floor:
            ranked = sorted(
                f["events"],
                key=lambda m: (
                    m.get("recall_count") or 0,
                    m.get("weight") or 0,
                    str(m.get("created_at") or ""),
                ),
                reverse=True,
            )
            # Prepend any durable rows (below the floor) so decisions aren't
            # silently dropped when we fall back to the event corpus.
            return f["durable"] + ranked, "activity"
        return None

    # Split ranked families: top_n (cost-capped) get an LLM summary; the rest of
    # the active families become count-only "listed" rows so none go invisible.
    budget = max(1, int(max_cost / _PER_CALL_COST_USD)) if max_cost else None
    to_summarize: list[tuple[dict, list[dict], str]] = []
    to_list: list[dict] = []
    for f in active:
        c = _corpus(f)
        if c and len(to_summarize) < top_n and (budget is None or len(to_summarize) < budget):
            to_summarize.append((f, c[0], c[1]))
        elif len(to_list) < listed_max:
            to_list.append(f)

    llm_sem = asyncio.Semaphore(_LLM_CONCURRENCY)

    async def _one(f: dict, corpus: list[dict], mode: str) -> str:
        async with llm_sem:
            truncated = len(corpus) > max_mems
            return await _summarize_agent(
                org_id=org_id,
                agent=f["agent"],
                mems=corpus[:max_mems],
                period=period,
                run_id=run_id,
                window_start=window_start,
                window_end=window_end,
                model=model,
                provider=provider,
                truncated=truncated,
                mode=mode,
                subagents=f["subagents"],
                source_count=f["total"],
            )

    statuses = await asyncio.gather(*(_one(f, c, m) for f, c, m in to_summarize), return_exceptions=True)
    generated = sum(1 for s in statuses if s in ("ok", "truncated", "activity"))
    skipped = sum(1 for s in statuses if s == "skipped")
    errored = sum(1 for s in statuses if isinstance(s, BaseException) or s == "errored")

    # Count-only tail: active families we didn't LLM-summarize get a listed row
    # (no narrative, no LLM) so an active agent is never fully invisible.
    listed = 0
    for f in to_list:
        try:
            await sc.upsert_agent_activity_digest(
                {
                    "run_id": run_id,
                    "tenant_id": org_id,
                    "fleet_id": f["agent"].get("fleet_id"),
                    "agent_id": f["parent"],
                    "period": period,
                    "window_start": window_start.isoformat(),
                    "window_end": window_end.isoformat(),
                    "narrative": None,
                    "sections": {},
                    "subagents": f["subagents"],
                    "source_count": f["total"],
                    "recall_count": None,
                    "model": None,
                    "status": "listed",
                    "error_detail": None,
                }
            )
            listed += 1
        except Exception as exc:
            errored += 1
            logger.warning("agent_digest: listed upsert failed for %s/%s: %r", org_id, f["parent"], exc)

    # Retention sweep, folded into the run (this org's latest run is always
    # fresh, so old runs age out). NOTE: an org that later DISABLES the digest
    # stops running and won't self-prune — acceptable for v1; a global sweep can
    # reclaim those later. A prune failure must not fail the generation.
    pruned = 0
    retention_days = int(config.get("retention_days") or 0)
    if retention_days > 0:
        cutoff = now - timedelta(days=retention_days)
        try:
            pruned = await sc.prune_agent_activity_digests(org_id, cutoff.isoformat())
        except Exception as exc:
            logger.warning("agent_digest: prune failed for %s: %r", org_id, exc)

    return {
        "org_id": org_id,
        "run_id": run_id,
        "agents": len(agents),
        "families": len(active),
        "generated": generated,
        "listed": listed,
        "skipped": skipped,
        "errored": errored,
        "pruned": pruned,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
    }


async def run_agent_digest(period: str = "day") -> dict:
    """Enumerate opted-in orgs and generate their digests (inline fanout).

    Called by the admin fanout endpoint (core-operations cron trigger). One org's
    failure never sinks the rest. Returns a bounded counts summary.
    """
    if period not in _PERIOD_DAYS:
        raise ValueError(f"invalid period {period!r}; use 'day' or 'week'")
    # Local import breaks a tenants ↔ storage_client import cycle at module load.
    from core_api.services.tenants import list_tenants_with_agent_digest_enabled

    org_ids = await list_tenants_with_agent_digest_enabled()
    now = datetime.now(UTC)
    org_sem = asyncio.Semaphore(_ORG_CONCURRENCY)

    async def _one(org_id: str) -> dict | None:
        async with org_sem:
            settings = await get_settings_for_display(org_id)
            config = settings.get("agent_digest") or {}
            if not config.get("enabled"):  # enumeration already filters; re-check
                return None
            return await generate_for_org(org_id, period, config, now=now)

    results = await asyncio.gather(*(_one(o) for o in org_ids), return_exceptions=True)
    completed = 0
    failed = 0
    digests = 0
    agent_listed = 0
    agent_skipped = 0
    agent_errors = 0
    for org_id, res in zip(org_ids, results, strict=True):
        if isinstance(res, BaseException):
            failed += 1
            logger.warning("agent_digest: org %s generation failed: %r", org_id, res)
        elif res:
            completed += 1
            digests += res.get("generated", 0)
            agent_listed += res.get("listed", 0)
            agent_skipped += res.get("skipped", 0)
            agent_errors += res.get("errored", 0)
    logger.info(
        "agent_digest run: period=%s orgs=%d completed=%d failed=%d "
        "digests=%d agent_listed=%d agent_skipped=%d agent_errors=%d",
        period,
        len(org_ids),
        completed,
        failed,
        digests,
        agent_listed,
        agent_skipped,
        agent_errors,
    )
    return {
        "period": period,
        "orgs": len(org_ids),
        "completed": completed,
        "failed": failed,
        "digests": digests,
        "agent_listed": agent_listed,
        "agent_skipped": agent_skipped,
        "agent_errors": agent_errors,
    }


# NON_COHESIVE_TITLE_REGEX is re-exported for callers/tests that want the exact
# server-side exclusion string alongside the client-side is_cohesive filter.
__all__ = ["run_agent_digest", "generate_for_org", "DIGEST_SCHEMA", "NON_COHESIVE_TITLE_REGEX"]
