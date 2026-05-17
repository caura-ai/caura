"""Recall service: search + LLM summarization into a concise context paragraph."""

import json as _json
import logging
import time
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from core_api.constants import (
    DEFAULT_SEARCH_TOP_K,
    MEMORY_RECALL_SUMMARY_MAX_TOKENS,
    MEMORY_RECALL_SUMMARY_TEMPERATURE,
)
from core_api.providers._retry import call_with_fallback
from core_api.services.memory_service import search_memories

logger = logging.getLogger(__name__)

RECALL_PROMPT = """\
I will give you several facts and observations from past interactions and ingested content. \
Please answer the question based on the relevant facts. Answer the question step by step: \
first extract all the relevant information, then reason over the information to get the answer.

When the question requires combining facts from different memories, trace the connection \
explicitly. Pay attention to dates within the facts — events described in past tense \
occurred before the date the memory was recorded.

Grounding rules — follow strictly:
- Quote only text that appears verbatim in the memories below.
- Do not invent identifiers, field names, dates, titles, or numbers. If a detail is not \
present in the memories, do not include it.
- If the memories do not contain enough information to answer, say so plainly. Do not \
infer beyond the evidence.

Memories:

{memories}

{reference_date_line}Question: {query}
Answer (step by step):"""


def _format_memories_for_prompt(memories: list) -> str:
    """Format memories as a JSON array for structured LLM consumption.

    Only fields that exist on the API response are exposed. No ordinal IDs, no
    renamed schema fields — the model must not be able to cite identifiers or
    field names that a caller cannot resolve.
    """
    items = []
    for m in memories:
        item: dict = {"type": m.memory_type}
        if m.title:
            item["title"] = m.title
        if m.status and m.status != "active":
            item["status"] = m.status
        content = m.content or ""
        ts = getattr(m, "ts_valid_start", None)
        if ts:
            date_str = ts[:10] if isinstance(ts, str) else ts.strftime("%Y-%m-%d")
            content = f"[{date_str}] {content}" if content else f"[{date_str}]"
        item["content"] = content or None
        items.append(item)
    return _json.dumps(items, indent=2, ensure_ascii=False)


async def recall(
    db: AsyncSession,
    tenant_id: str,
    query: str,
    fleet_ids: list[str] | None = None,
    filter_agent_id: str | None = None,
    caller_agent_id: str | None = None,
    memory_type_filter: str | None = None,
    status_filter: str | None = None,
    top_k: int = DEFAULT_SEARCH_TOP_K,
    valid_at: datetime | None = None,
    diagnostic: bool = False,
    readable_tenant_ids: list[str] | None = None,
) -> dict:
    """Search memories and synthesize a context summary.

    Returns: {"query": ..., "summary": ..., "memory_count": ..., "memories": [...], "recall_ms": ...}
    """
    t0 = time.perf_counter()

    # Search for relevant memories
    from core_api.services.organization_settings import resolve_config

    config = await resolve_config(db, tenant_id)
    diagnostic_ctx: dict = {} if diagnostic else {}
    memories = await search_memories(
        db,
        tenant_id=tenant_id,
        query=query,
        fleet_ids=fleet_ids,
        filter_agent_id=filter_agent_id,
        caller_agent_id=caller_agent_id,
        memory_type_filter=memory_type_filter,
        status_filter=status_filter,
        top_k=top_k,
        valid_at=valid_at,
        recall_boost=config.recall_boost,
        graph_expand=config.graph_expand,
        tenant_config=config,
        diagnostic=diagnostic,
        diagnostic_ctx=diagnostic_ctx if diagnostic else None,
        readable_tenant_ids=readable_tenant_ids,
    )

    if not memories:
        resp = {
            "query": query,
            "summary": "No relevant context found.",
            "memory_count": 0,
            "memories": [],
            "recall_ms": int((time.perf_counter() - t0) * 1000),
        }
        if diagnostic:
            resp["diagnostic"] = {
                "recall_prompt": None,
                "recall_model": None,
                "recall_provider": None,
                "all_candidates": diagnostic_ctx.get("all_candidates", []),
                "top_k_used": top_k,
                "retrieval_strategy": diagnostic_ctx.get("retrieval_strategy"),
                "search_params": {
                    k: (float(v) if isinstance(v, (int, float)) else v)
                    for k, v in diagnostic_ctx.get("search_params", {}).items()
                },
            }
        return resp

    # Sort chronologically so the LLM sees a natural timeline
    _DT_MIN_UTC = datetime.min.replace(tzinfo=UTC)
    memories.sort(key=lambda m: getattr(m, "ts_valid_start", None) or _DT_MIN_UTC)

    # Format memories for the LLM
    memories_text = _format_memories_for_prompt(memories)
    if valid_at:
        reference_date_line = f"Current Date: {valid_at.strftime('%Y-%m-%d')}\n"
    else:
        reference_date_line = ""

    provider = config.recall_provider

    if not config.recall_enabled:
        resp = {
            "query": query,
            "summary": "Recall summarization is disabled.",
            "memory_count": len(memories),
            "memories": [m.model_dump(mode="json") for m in memories],
            "recall_ms": int((time.perf_counter() - t0) * 1000),
        }
        if diagnostic:
            resp["diagnostic"] = {
                "recall_prompt": None,
                "recall_model": None,
                "recall_provider": provider,
                "all_candidates": diagnostic_ctx.get("all_candidates", []),
                "top_k_used": top_k,
                "retrieval_strategy": diagnostic_ctx.get("retrieval_strategy"),
                "search_params": {
                    k: (float(v) if isinstance(v, (int, float)) else v)
                    for k, v in diagnostic_ctx.get("search_params", {}).items()
                },
            }
        return resp

    prompt = RECALL_PROMPT.format(
        query=query, memories=memories_text, reference_date_line=reference_date_line
    )

    def _fake_recall() -> str:
        """No-LLM fallback: join top memory contents."""
        return " ".join(m.content[:100] for m in memories[:3])

    async def _do_recall(llm) -> str:
        return await llm.complete_text(
            prompt,
            temperature=MEMORY_RECALL_SUMMARY_TEMPERATURE,
            max_tokens=MEMORY_RECALL_SUMMARY_MAX_TOKENS,
        )

    recall_model = getattr(config, "recall_model", None)
    summary = await call_with_fallback(
        primary_provider_name=provider,
        call_fn=_do_recall,
        fake_fn=_fake_recall,
        tenant_config=config,
        service_label="recall",
        model_override=recall_model,
        timeout=30.0,
    )

    recall_ms = int((time.perf_counter() - t0) * 1000)

    result = {
        "query": query,
        "summary": summary,
        "memory_count": len(memories),
        "memories": [m.model_dump(mode="json") for m in memories],
        "recall_ms": recall_ms,
    }

    if diagnostic:
        recall_model = getattr(config, "recall_model", None)
        result["diagnostic"] = {
            "recall_prompt": prompt,
            "recall_model": recall_model or "default",
            "recall_provider": provider,
            "all_candidates": diagnostic_ctx.get("all_candidates", []),
            "top_k_used": top_k,
            "retrieval_strategy": diagnostic_ctx.get("retrieval_strategy"),
            "search_params": {
                k: (float(v) if isinstance(v, (int, float)) else v)
                for k, v in diagnostic_ctx.get("search_params", {}).items()
            },
        }

    return result
