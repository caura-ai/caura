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


def _build_prompt(agent: dict, mems: list[dict], period: str) -> str:
    name = agent.get("display_name") or agent.get("agent_id")
    lines = []
    for m in mems:
        title = m.get("title") or (m.get("metadata") or {}).get("summary") or "(untitled)"
        lines.append(
            f"- [{m.get('memory_type', '?')}] {title}"
            f" (recalled {m.get('recall_count') or 0}x, {str(m.get('created_at'))[:10]})"
        )
    corpus = "\n".join(lines)
    return (
        f"You are summarizing what the AI agent '{name}' did over the past "
        f"{'day' if period == 'day' else 'week'}, based ONLY on its durable "
        f"memories below. Do not invent anything not grounded in them. Quantify "
        f"where natural (e.g. 'made 3 decisions'). If the memories are thin, say "
        f"so briefly.\n\n"
        f"Memories ({len(mems)}):\n{corpus}\n\n"
        f"Return JSON: narrative (2-4 sentence prose recap), decisions, shipped, "
        f"learned, open_threads (short bullet strings; omit or empty if none), "
        f"and confidence (0-1)."
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
) -> str:
    """Build (via LLM) and persist one agent's digest row. Returns its status."""
    prompt = _build_prompt(agent, mems, period)

    async def _call(llm: Any) -> dict:
        return await llm.complete_json(prompt, response_schema=DIGEST_SCHEMA)

    # call_with_fallback silently drops to _fake() when the real (and fallback)
    # provider both fail. This flag lets us mark the row so a placeholder
    # narrative is never mistaken for a real LLM summary.
    used_fallback = False

    def _fake() -> dict:
        nonlocal used_fallback
        used_fallback = True
        return {"narrative": f"{agent.get('agent_id')} recorded {len(mems)} durable memories."}

    status = "ok"
    if truncated:
        status = "truncated"
    narrative: str | None = None
    sections: dict = {}
    error: str | None = None
    try:
        raw = await call_with_fallback(
            provider,
            _call,
            _fake,
            tenant_config=None,
            service_label="agent-digest",
            model_override=model,
        )
        narrative = raw.get("narrative") or None
        sections = {k: raw.get(k) or [] for k in _SECTION_KEYS}
        if used_fallback:
            # Template narrative — overrides truncated so the caller can tell
            # it's a placeholder, not a real (if partial) summary.
            status = "fallback"
        elif not narrative:
            # The provider uses strict=False (no server-side schema enforcement)
            # and there's no Pydantic guardrail here, so a real response can omit
            # the narrative — treat that as an error, not a NULL "ok" row.
            status = "error"
            error = "LLM returned no narrative"
    except Exception as exc:  # storage/unexpected — real errors, not LLM fallback
        status, error = "error", repr(exc)
        logger.warning("agent_digest: summarize failed for %s/%s: %r", org_id, agent.get("agent_id"), exc)

    row = {
        "run_id": run_id,
        "tenant_id": org_id,
        "fleet_id": agent.get("fleet_id"),
        "agent_id": agent["agent_id"],
        "period": period,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "narrative": narrative,
        "sections": sections,
        "source_count": len(mems),
        "recall_count": sum(m.get("recall_count") or 0 for m in mems),
        "model": model,
        "status": status,
        "error_detail": error,
    }
    try:
        await get_storage_client().upsert_agent_activity_digest(row)
    except Exception as exc:
        # Re-raise so gather still counts this agent as errored; log for traceability.
        logger.warning(
            "agent_digest: upsert failed for %s/%s: %r",
            org_id,
            agent.get("agent_id"),
            exc,
        )
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
    min_activity = int(config.get("min_activity_threshold") or 3)
    model = config.get("model") or "gpt-5.4-mini"
    provider = config.get("provider") or "openai"
    max_cost = float(config.get("max_cost_per_run_usd") or 0)

    agents = await sc.list_agents(org_id)

    # Fetch each agent's cohesive window memories (cheap; bounded concurrency).
    fetch_sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _fetch(agent: dict) -> tuple[dict, list[dict]]:
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
            cohesive = [m for m in (rows or []) if is_cohesive(m)]
            return agent, cohesive

    fetched = await asyncio.gather(*(_fetch(a) for a in agents), return_exceptions=True)
    candidates: list[tuple[dict, list[dict]]] = []
    for res in fetched:
        if isinstance(res, BaseException):
            logger.warning("agent_digest: fetch failed for an agent in %s: %r", org_id, res)
            continue
        agent, cohesive = res
        if len(cohesive) >= min_activity:
            candidates.append((agent, cohesive))
    candidates.sort(key=lambda ac: len(ac[1]), reverse=True)

    selected = candidates[:top_n]
    # Turn the USD cap into a call budget (rough per-call estimate).
    if max_cost:
        budget = max(1, int(max_cost / _PER_CALL_COST_USD))
        if len(selected) > budget:
            logger.warning("agent_digest: org %s cost cap trims %d→%d agents", org_id, len(selected), budget)
            selected = selected[:budget]

    llm_sem = asyncio.Semaphore(_LLM_CONCURRENCY)

    async def _one(agent: dict, mems: list[dict]) -> str:
        async with llm_sem:
            truncated = len(mems) > max_mems
            return await _summarize_agent(
                org_id=org_id,
                agent=agent,
                mems=mems[:max_mems],
                period=period,
                run_id=run_id,
                window_start=window_start,
                window_end=window_end,
                model=model,
                provider=provider,
                truncated=truncated,
            )

    statuses = await asyncio.gather(*(_one(a, m) for a, m in selected), return_exceptions=True)
    generated = sum(1 for s in statuses if s in ("ok", "truncated"))
    fallback = sum(1 for s in statuses if s == "fallback")
    errored = sum(1 for s in statuses if isinstance(s, BaseException) or s == "error")

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
        "candidates": len(candidates),
        "generated": generated,
        "fallback": fallback,
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
    agent_fallbacks = 0
    agent_errors = 0
    for org_id, res in zip(org_ids, results, strict=True):
        if isinstance(res, BaseException):
            failed += 1
            logger.warning("agent_digest: org %s generation failed: %r", org_id, res)
        elif res:
            completed += 1
            digests += res.get("generated", 0)
            agent_fallbacks += res.get("fallback", 0)
            agent_errors += res.get("errored", 0)
    logger.info(
        "agent_digest run: period=%s orgs=%d completed=%d failed=%d "
        "digests=%d agent_fallbacks=%d agent_errors=%d",
        period,
        len(org_ids),
        completed,
        failed,
        digests,
        agent_fallbacks,
        agent_errors,
    )
    return {
        "period": period,
        "orgs": len(org_ids),
        "completed": completed,
        "failed": failed,
        "digests": digests,
        "agent_fallbacks": agent_fallbacks,
        "agent_errors": agent_errors,
    }


# NON_COHESIVE_TITLE_REGEX is re-exported for callers/tests that want the exact
# server-side exclusion string alongside the client-side is_cohesive filter.
__all__ = ["run_agent_digest", "generate_for_org", "DIGEST_SCHEMA", "NON_COHESIVE_TITLE_REGEX"]
