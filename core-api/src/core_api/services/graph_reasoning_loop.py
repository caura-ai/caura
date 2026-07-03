"""Bounded agentic reasoning loop over typed graph-query tools (RL-02).

Opt-in enhancement to memclaw_recall's fixed single-pass hybrid search. Each
turn asks the tenant's configured LLM provider to pick one of the typed tools
in graph_query_tools.py (or "done"), executes it, and accumulates a
boosted-memory set. Bounded by GRAPH_REASONING_MAX_ITERATIONS and wrapped in
asyncio.wait_for — any exception or timeout returns whatever was accumulated
so far, mirroring the existing entity-boost pipeline's fallback-on-failure
pattern. Never raises to the caller.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from uuid import UUID

from core_api.providers._retry import call_with_fallback
from core_api.services.graph_query_tools import (
    TOOL_SPECS,
    query_by_entity_links,
    query_by_graph_context,
    query_by_keyword,
    query_by_time_range,
)

logger = logging.getLogger(__name__)

# Not per-org configurable in v1, per plan scope decision.
GRAPH_REASONING_MAX_ITERATIONS = 3
GRAPH_REASONING_STEP_TIMEOUT_SECONDS = 10.0
GRAPH_REASONING_TOTAL_TIMEOUT_SECONDS = 25.0

# Direct keyword/entity-link matches are the most confident signal;
# graph-context expansion and time-range listing are one hop removed.
_TOOL_BOOST = {
    "query_by_keyword": 1.2,
    "query_by_entity_links": 1.2,
    "query_by_graph_context": 1.1,
    "query_by_time_range": 1.1,
}

_TOOL_CHOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {"type": "string"},
        "args": {"type": "object"},
    },
    "required": ["tool"],
}

_PROMPT_TEMPLATE = """\
You are selecting graph-query tools to answer a question over a personal memory graph.
Question: {query}

Available tools:
{tool_list}

Steps taken so far:
{trace}

Entities found so far: {entity_count}
Memories found so far: {memory_count}

Pick the single most useful next tool, or "done" if further tool calls would not \
add new information. Respond with ONLY a JSON object: {{"tool": "<tool_name_or_done>", "args": {{}}}}"""


def _fake_tool_choice() -> dict:
    """No-LLM fallback: stop immediately, accumulate nothing further."""
    return {"tool": "done", "args": {}}


async def run_reasoning_loop(
    query: str,
    tenant_id: str,
    config,
    seed_entity_ids: list[str],
    *,
    fleet_ids: list[str] | None = None,
    reference_datetime: datetime | None = None,
) -> tuple[set[str], float, list[dict]]:
    """Run the bounded reasoning loop. Never raises — returns partial results on failure/timeout.

    Returns (boosted_memory_ids, boost_factor, trace): boosted_memory_ids is the
    union of memory ids surfaced by whichever tools the loop chose to run;
    boost_factor is the single strongest per-tool confidence seen this run
    (>=1.0), meant to be combined with the existing fixed-pass graph boost via
    max(); trace is a list of {turn, tool, args, result_count} dicts.
    """
    try:
        return await asyncio.wait_for(
            _run_loop(query, tenant_id, config, seed_entity_ids, fleet_ids, reference_datetime),
            timeout=GRAPH_REASONING_TOTAL_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning("graph reasoning loop failed, returning empty boost", exc_info=True)
        return set(), 0.0, [{"turn": 0, "tool": None, "args": {}, "result_count": 0}]


async def _run_loop(
    query: str,
    tenant_id: str,
    config,
    seed_entity_ids: list[str],
    fleet_ids: list[str] | None,
    reference_datetime: datetime | None,
) -> tuple[set[str], float, list[dict]]:
    boosted_memory_ids: set[UUID] = set()
    entity_ids: set[UUID] = {UUID(eid) for eid in seed_entity_ids} if seed_entity_ids else set()
    trace: list[dict] = []
    used_tools: set[str] = set()
    boost_factor = 0.0

    provider = getattr(config, "recall_provider", "fake")
    recall_enabled = getattr(config, "recall_enabled", True)
    if not recall_enabled:
        return set(), 0.0, [{"turn": 0, "tool": None, "args": {}, "result_count": 0}]

    for turn in range(GRAPH_REASONING_MAX_ITERATIONS):
        tool_name, llm_args = await _choose_tool(
            query, provider, config, entity_ids, boosted_memory_ids, trace
        )

        if tool_name == "done" or tool_name not in TOOL_SPECS:
            trace.append({"turn": turn, "tool": tool_name, "args": llm_args, "result_count": 0})
            break
        if tool_name in used_tools:
            trace.append({"turn": turn, "tool": tool_name, "args": llm_args, "result_count": 0})
            break
        used_tools.add(tool_name)

        found_memory_ids, found_entity_ids, _note = await _dispatch(
            tool_name, query, tenant_id, fleet_ids, entity_ids, reference_datetime
        )
        result_count = len(found_memory_ids) + len(found_entity_ids)
        trace.append({"turn": turn, "tool": tool_name, "args": llm_args, "result_count": result_count})

        entity_ids |= found_entity_ids
        boosted_memory_ids |= found_memory_ids
        boost_factor = max(boost_factor, _TOOL_BOOST[tool_name])

        if result_count == 0:
            # No new signal — further iterations are unlikely to help.
            break

    return {str(mid) for mid in boosted_memory_ids}, boost_factor, trace


async def _choose_tool(
    query: str,
    provider: str,
    config,
    entity_ids: set[UUID],
    boosted_memory_ids: set[UUID],
    trace: list[dict],
) -> tuple[str, dict]:
    tool_list = "\n".join(f"- {name}: {desc}" for name, desc in TOOL_SPECS.items())
    trace_lines = "\n".join(f"{t['turn']}: {t['tool']} ({t['result_count']} results)" for t in trace)
    prompt = _PROMPT_TEMPLATE.format(
        query=query,
        tool_list=tool_list,
        trace=trace_lines or "(none yet)",
        entity_count=len(entity_ids),
        memory_count=len(boosted_memory_ids),
    )

    async def _pick_tool(llm) -> dict:
        return await llm.complete_json(prompt, temperature=0.0, response_schema=_TOOL_CHOICE_SCHEMA)

    try:
        choice = await call_with_fallback(
            primary_provider_name=provider,
            call_fn=_pick_tool,
            fake_fn=_fake_tool_choice,
            tenant_config=config,
            service_label="graph-reasoning",
            timeout=GRAPH_REASONING_STEP_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning("graph reasoning tool-choice call failed", exc_info=True)
        return "done", {}

    choice = choice or {}
    return choice.get("tool", "done"), choice.get("args", {}) or {}


async def _dispatch(
    tool_name: str,
    query: str,
    tenant_id: str,
    fleet_ids: list[str] | None,
    entity_ids: set[UUID],
    reference_datetime: datetime | None,
) -> tuple[set[UUID], set[UUID], str]:
    # Tool arguments are derived from accumulated loop state, not taken from
    # the LLM's `args` response — trusting LLM-supplied entity ids risks
    # dispatching on hallucinated UUIDs. The LLM's `args` is still parsed and
    # recorded in the trace for diagnostic purposes.
    if tool_name == "query_by_keyword":
        return await query_by_keyword(query, tenant_id, fleet_ids)
    if tool_name == "query_by_graph_context":
        return await query_by_graph_context(list(entity_ids), tenant_id, fleet_ids)
    if tool_name == "query_by_time_range":
        return await query_by_time_range(query, tenant_id, fleet_ids, reference_datetime)
    return await query_by_entity_links(list(entity_ids))
