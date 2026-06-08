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


async def summarize_memories(
    memories: list,
    query: str,
    config,
    *,
    valid_at: datetime | None = None,
    diagnostic: bool = False,
    diagnostic_ctx: dict | None = None,
    top_k: int = DEFAULT_SEARCH_TOP_K,
    t0: float | None = None,
) -> dict:
    """LLM-only summarization step. No DB access.

    Audit finding P3: ``memclaw_recall`` previously held the
    ``_mcp_session()`` open across the multi-second LLM round-trip,
    pinning a pooled DB connection. This helper takes already-fetched
    memories + the resolved tenant config and produces the same dict
    shape the legacy ``recall()`` wrapper returned, so the MCP tool can
    exit the session block before invoking it.

    ``t0`` is the caller's ``time.perf_counter()`` checkpoint for the
    surrounding handler — passing it preserves the original "recall_ms
    measures end-to-end from auth-pass" semantics. Omitted callers get
    a fresh checkpoint that only times the summary itself.
    """
    if t0 is None:
        t0 = time.perf_counter()
    diagnostic_ctx = diagnostic_ctx or {}

    if not memories:
        resp = {
            "query": query,
            "summary": "No relevant context found.",
            "memory_count": 0,
            # C4 — ``items`` aliases ``memories`` so consumers that
            # pattern-match on /search's shape don't silently get zero
            # results when hitting /recall instead. Both keys point at
            # the same list (here trivially empty).
            "memories": [],
            "items": [],
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

    # Sort chronologically so the LLM sees a natural timeline. Callers
    # may have already sorted; the operation is idempotent.
    _DT_MIN_UTC = datetime.min.replace(tzinfo=UTC)
    memories.sort(key=lambda m: getattr(m, "ts_valid_start", None) or _DT_MIN_UTC)

    memories_text = _format_memories_for_prompt(memories)
    if valid_at:
        reference_date_line = f"Current Date: {valid_at.strftime('%Y-%m-%d')}\n"
    else:
        reference_date_line = ""

    provider = config.recall_provider

    if not config.recall_enabled:
        # C4 — materialise once; alias under both ``memories`` and
        # ``items`` keys so consumers built against /search's shape see
        # the same list.
        _memories_dumps = [m.model_dump(mode="json") for m in memories]
        resp = {
            "query": query,
            "summary": "Recall summarization is disabled.",
            "memory_count": len(memories),
            "memories": _memories_dumps,
            "items": _memories_dumps,
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

    # C4 — materialise once; alias under both ``memories`` and ``items``.
    _memories_dumps = [m.model_dump(mode="json") for m in memories]
    result = {
        "query": query,
        "summary": summary,
        "memory_count": len(memories),
        "memories": _memories_dumps,
        "items": _memories_dumps,
        "recall_ms": recall_ms,
    }

    if diagnostic:
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

    Returns: {"query": ..., "summary": ..., "memory_count": ..., "memories": [...], "items": [...], "recall_ms": ...}

    ``memories`` and ``items`` both reference the same list — the ``items``
    alias was added by C4 so consumers built against ``/search``'s
    response shape (which keys on ``items``) don't silently get zero
    results when hitting ``/recall``.

    Thin wrapper over ``search_memories`` + ``summarize_memories``. MCP
    tool callers that already hold the search results and tenant config
    should invoke ``summarize_memories`` directly so they can close
    their DB session before the LLM round-trip (audit P3). This wrapper
    is retained for the REST surface and other callers that prefer the
    one-shot ergonomics.
    """
    t0 = time.perf_counter()

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
    return await summarize_memories(
        memories,
        query,
        config,
        valid_at=valid_at,
        diagnostic=diagnostic,
        diagnostic_ctx=diagnostic_ctx,
        top_k=top_k,
        t0=t0,
    )
