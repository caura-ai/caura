"""Recall service: search + LLM summarization into a concise context paragraph."""

import json as _json
import logging
import re
import time
from datetime import UTC, datetime

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
Answer the question using ONLY those memories as your source of truth — do not use any outside \
or world knowledge. Answer step by step: first extract the relevant facts from the memories, \
then reason over only those facts to reach the answer.

When the question requires combining facts from different memories, trace the connection \
explicitly. Pay attention to dates within the facts — events described in past tense \
occurred before the date the memory was recorded.

{premise_guard_block}Grounding rules — follow strictly:
- Use only the memories below. Do not add any fact, and do not rely on prior or world knowledge.
- Every name, date, number, title, field name, and identifier in your answer MUST appear \
verbatim in the memories. Never invent, estimate, approximate, or complete a missing value — \
e.g. do not supply a specific completion date if the memories don't state one.
- When using quotation marks, quote only text that appears word-for-word in the memories; do \
not paraphrase inside quotes.
- If the memories don't contain a detail the question asks for, say it is not recorded rather \
than supplying one. If they don't contain enough to answer at all, say so plainly. Do not \
infer beyond the evidence.

After your step-by-step reasoning, end your reply with one final line formatted exactly:
**Answer:** <your answer>
This line must contain the complete answer on its own.

Memories:

{memories}

{reference_date_line}Question: {query}
Answer (step by step):"""

# A64 — the premise guard, org-opt-in via ``recall.premise_guard``. Wording is
# the benchmark-tuned v2: the first sentence buys the STALE-T2 gain (31%->71%
# overall — an agent should not comply with a premise its own memories refute),
# the second closes the over-abstention the v1 wording caused on
# knowledge-update questions (answerable questions turned into "not enough
# information"). Change only with a fresh control pair on the 67-q regression
# sample (see benchmark/a57-recall-experiments-findings.md).
PREMISE_GUARD_BLOCK = """\
Before answering, check whether the question rests on an assumption about the \
user's current situation that the memories contradict or no longer support \
(they may imply a change without stating it outright). If so, point out that \
the assumption appears outdated and answer for the user's actual current \
situation instead of going along with the premise. If the memories do answer \
the question, answer it — do not abstain merely because a memory is older; \
flag only assumptions the memories actually contradict or supersede.

"""

# WT-1 — the prompt above deliberately elicits step-by-step reasoning before the
# answer (it is load-bearing for recall accuracy on LoCoMo/LongMemEval), but the
# raw completion used to be surfaced as ``summary`` unfiltered: callers paid ~5x
# the tokens and had to string-parse for the trailing "**Answer:**" line
# themselves. The marker is now extracted server-side; the reasoning scaffold
# stays in the completion (and in ``diagnostic.recall_raw``), never in ``summary``.
#
# Tolerant of the variants models actually emit: ``**Answer:**`` / ``**Answer**:``
# anywhere in the text, or a plain ``Answer:`` at the start of a line. The plain
# form is line-anchored so prose like "the answer: ..." inside the reasoning
# cannot false-positive mid-sentence.
_ANSWER_MARKER = re.compile(
    r"""
    (?:
        \*\*[ \t]*Answer[ \t]*:[ \t]*\*\*     # **Answer:**
      | \*\*[ \t]*Answer[ \t]*\*\*[ \t]*:     # **Answer**:
      | ^[ \t]*Answer[ \t]*:                  # Answer: at line start
    )
    [ \t]*
    """,
    re.MULTILINE | re.VERBOSE,
)


def _extract_final_answer(completion: str) -> str:
    """Return the text after the LAST answer marker, stripped.

    Fail open: with no marker (older prompt in flight, a model that ignored the
    format instruction, the ``_fake_recall`` fallback, or a completion truncated
    by ``MEMORY_RECALL_SUMMARY_MAX_TOKENS`` before the marker was emitted) the
    full completion is returned unchanged — a verbose summary beats an empty or
    mangled one. Ditto when the marker is the last thing in the completion and
    nothing follows it.

    LAST occurrence, not first: the reasoning steps may legitimately quote or
    rehearse an "Answer:" line before committing to the final one.
    """
    matches = list(_ANSWER_MARKER.finditer(completion))
    if not matches:
        return completion
    answer = completion[matches[-1].end() :].strip()
    return answer or completion


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

    Audit finding P3: ``caura_recall`` previously held the
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
                "recall_raw": None,
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
                "recall_raw": None,
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
        query=query,
        memories=memories_text,
        reference_date_line=reference_date_line,
        # A64 — off (the default) leaves the prompt byte-identical to pre-A64.
        premise_guard_block=(PREMISE_GUARD_BLOCK if getattr(config, "recall_premise_guard", False) else ""),
    )

    def _fake_recall() -> str:
        """No-LLM fallback: the top memory contents, labelled as unsynthesized.

        Unlike the other fallbacks in this sweep this one is a READ path — the
        string lands in the response's ``summary`` and is never persisted — so
        returning something beats returning nothing. What it must not do is pass
        three truncated memory fragments off as a synthesised answer, which is what
        an unlabelled join did.

        Marked in the text rather than via a side-channel field because the caller
        surfaces ``summary`` verbatim to whoever asked; a flag they don't read is
        the same as no flag. Same shape as ``interview_service._fake_report``'s
        "(LLM unavailable; unsynthesized)".
        """
        joined = " ".join(m.content[:100] for m in memories[:3])
        if not joined:
            return "No summary available (no LLM provider answered)."
        return f"(LLM unavailable; top {min(len(memories), 3)} memories unsynthesized) {joined}"

    async def _do_recall(llm) -> str:
        return await llm.complete_text(
            prompt,
            temperature=MEMORY_RECALL_SUMMARY_TEMPERATURE,
            max_tokens=MEMORY_RECALL_SUMMARY_MAX_TOKENS,
        )

    recall_model = getattr(config, "recall_model", None)
    completion = await call_with_fallback(
        primary_provider_name=provider,
        call_fn=_do_recall,
        fake_fn=_fake_recall,
        tenant_config=config,
        service_label="recall",
        model_override=recall_model,
        # Recall is a latency-sensitive read path. Cap the per-attempt LLM timeout AND
        # don't retry the same provider: with the default 2 attempts across primary +
        # fallback, a slow provider stacks past the 45s request-timeout budget and
        # surfaces as a Cloud Run "malformed response / connection error" 503. One
        # primary attempt (15s), then one fallback attempt (15s), then _fake_recall
        # keeps the worst case ~30s and fails fast instead of hanging.
        timeout=15.0,
        max_attempts=1,
        # The same 15s, declared as a budget rather than implied by the two
        # values above. That arithmetic held only while ``max_attempts`` stayed
        # 1: restoring the default — an entirely reasonable-looking change —
        # silently doubled the worst case to ~60s, with nothing but this
        # comment to say otherwise. ``budget_s`` bounds the wall clock per
        # provider whatever the attempt count is, so the ~30s promise is now
        # structural.
        budget_s=15.0,
    )

    # WT-1 — surface only the final answer as ``summary``; the step-by-step
    # scaffold stays available under ``diagnostic.recall_raw``.
    summary = _extract_final_answer(completion)

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
            # WT-1 — the unfiltered completion (reasoning scaffold + marker),
            # for debugging what the extraction saw.
            "recall_raw": completion,
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
    min_similarity: float | None = None,
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

    config = await resolve_config(tenant_id)
    diagnostic_ctx: dict = {} if diagnostic else {}
    memories = await search_memories(
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
        entity_retrieval=config.entity_retrieval,
        tenant_config=config,
        diagnostic=diagnostic,
        diagnostic_ctx=diagnostic_ctx if diagnostic else None,
        readable_tenant_ids=readable_tenant_ids,
        min_similarity=min_similarity,
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
