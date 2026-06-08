"""LLM-powered memory enrichment service — moved from
``core_api.services.memory_enrichment`` (CAURA-595).

Public surface:

* :func:`enrich_memory` — async entrypoint with 3-tier fallback.
* :func:`fake_enrich` — keyword-heuristic fallback used both as the
  ``"fake"`` provider and as the last-resort safety net.

Provider resolution mirrors the embedding service: tenant config first,
``ENTITY_EXTRACTION_PROVIDER`` env var fallback. The service never
raises — every error path lands on the heuristic fallback so the write
pipeline can always continue.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, date, datetime

from dateutil import parser as dateutil_parser
from dateutil.parser import ParserError

from common.enrichment._prompts import ENRICHMENT_PROMPT
from common.enrichment.constants import (
    DEFAULT_MEMORY_TYPE,
    MEMORY_STATUSES,
    MEMORY_TYPES,
    SERVER_RESERVED_MEMORY_TYPES,
    MemoryType,
)
from common.enrichment.schema import EnrichmentResult
from common.llm.protocols import LLMProvider
from common.llm.retry import call_with_fallback
from common.provider_names import ProviderName

logger = logging.getLogger(__name__)


def _parse_temporal(val: str) -> datetime | None:
    """Parse a temporal string to UTC datetime, returning None on failure."""
    try:
        dt = dateutil_parser.parse(val, fuzzy=False)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        # Sanity check: not before 1970, not after 2100
        if dt.year < 1970 or dt.year > 2100:
            logger.warning("Temporal value out of range: %s", val)
            return None
        return dt
    except (ValueError, OverflowError, ParserError):
        logger.warning("Failed to parse temporal value: %s", val)
        return None


def _validate_enrichment(raw: dict, llm_ms: int) -> EnrichmentResult:
    """Validate and sanitize raw LLM enrichment output."""
    # CAURA-651: defense-in-depth against an LLM provider returning a
    # JSON list (or any non-dict shape) when the schema asks for an
    # object. Vertex / Gemini / OpenAI all raise their own
    # ``*ResponseShapeError(ValueError)`` at the JSON parse boundary;
    # this guard is the catch-all in case a future provider misses
    # that pattern. ``ValueError`` rather than ``TypeError`` because
    # this is bad data from an external system, not a programmer's
    # type-mismatch bug, and it stays consistent with the
    # ``ResponseShapeError(ValueError)`` hierarchy so a single
    # ``except ValueError`` catches both layers.
    if not isinstance(raw, dict):
        raise ValueError(
            f"enrichment LLM returned a JSON {type(raw).__name__} where a dict was expected"
        )
    # Backstop for CAURA-699: the prompt no longer offers the server-reserved
    # types (insight/outcome/rule), so a reserved value here means a
    # hallucinated/out-of-vocab classification — treat it like any other
    # invalid type and fall to the default. Reserved types are authored only
    # by internal flows that set ``memory_type`` explicitly and bypass this
    # classifier path entirely.
    mt = raw.get("memory_type")
    if mt not in MEMORY_TYPES or mt in SERVER_RESERVED_MEMORY_TYPES:
        raw["memory_type"] = DEFAULT_MEMORY_TYPE
    if raw.get("status") not in MEMORY_STATUSES:
        raw["status"] = "active"
    # Guard against the LLM emitting either a JSON null
    # (``raw.get("weight", 0.7)`` returns ``None``, not the default —
    # the key exists, the value is ``None``) or a non-numeric string
    # (``"high"``, ``"medium"``). Either crashes the entire
    # enrichment, which then silently falls through to ``fake_enrich``
    # via the outer caller's exception handling.
    #
    # Explicit ``is None`` test rather than ``or 0.7`` — ``or`` would
    # promote a legitimate ``weight=0.0`` (zero-salience content) to
    # the default, since ``0.0`` is falsy in Python.
    weight_raw = raw.get("weight")
    try:
        weight = float(weight_raw if weight_raw is not None else 0.7)
    except (TypeError, ValueError):
        weight = 0.7
    raw["weight"] = max(0.0, min(1.0, weight))
    raw["title"] = str(raw.get("title", ""))[:80]
    parsed_ts: dict[str, datetime | None] = {}
    for ts_field in ("ts_valid_start", "ts_valid_end"):
        val = raw.get(ts_field)
        if val and isinstance(val, str):
            parsed_ts[ts_field] = _parse_temporal(val)
        else:
            parsed_ts[ts_field] = None
        raw[ts_field] = parsed_ts[ts_field].isoformat() if parsed_ts[ts_field] else None
    # Ensure end > start; drop invalid end
    start, end = parsed_ts["ts_valid_start"], parsed_ts["ts_valid_end"]
    if start and end and end <= start:
        logger.warning(
            "ts_valid_end (%s) <= ts_valid_start (%s), dropping end",
            raw["ts_valid_end"],
            raw["ts_valid_start"],
        )
        raw["ts_valid_end"] = None

    # Episodes never expire: the event is a permanent historical fact.
    # Safety net in case the LLM ignores the prompt rule.
    if raw.get("memory_type") == "episode" and raw.get("ts_valid_end") is not None:
        logger.warning(
            "Stripping ts_valid_end=%s from episode memory (episodes don't expire)",
            raw["ts_valid_end"],
        )
        raw["ts_valid_end"] = None

    hint = raw.get("retrieval_hint")
    if isinstance(hint, str):
        raw["retrieval_hint"] = hint.strip()[:200]
    else:
        raw["retrieval_hint"] = ""

    facts_raw = raw.get("atomic_facts")
    cleaned_facts: list[dict] = []
    if isinstance(facts_raw, list):
        for f in facts_raw:
            if not isinstance(f, dict):
                continue
            fc = f.get("content")
            if not isinstance(fc, str) or not fc.strip():
                continue
            st = f.get("suggested_type")
            if (
                not isinstance(st, str)
                or st not in MEMORY_TYPES
                or st in SERVER_RESERVED_MEMORY_TYPES
            ):
                st = DEFAULT_MEMORY_TYPE
            fh = f.get("retrieval_hint")
            if not isinstance(fh, str):
                fh = ""
            cleaned_facts.append(
                {
                    "content": fc.strip()[:10000],
                    "suggested_type": st,
                    "retrieval_hint": fh.strip()[:200],
                }
            )
    # Any fact the enricher calls out separately deserves its own child
    # memory — even just one — because by definition the LLM considered
    # it distinct enough from the main title/summary to name explicitly.
    raw["atomic_facts"] = cleaned_facts if cleaned_facts else None
    raw["contains_pii"] = bool(raw.get("contains_pii", False))
    raw["pii_types"] = raw.get("pii_types") or []
    raw["llm_ms"] = llm_ms
    return EnrichmentResult(**raw)


def fake_enrich(content: str) -> EnrichmentResult:
    """Keyword heuristic fallback used by the ``"fake"`` provider and
    as the last-resort safety net when all real providers fail.

    ``llm_ms=0`` is hardcoded below — no LLM call was made, so 0 is
    the semantically correct value AND the reliable proxy the worker's
    ``_build_patch`` uses to distinguish heuristic results from real
    LLM results (it gates several "clear stale storage value" code
    paths on ``result.llm_ms > 0``). The previous wall-clock-derived
    value would occasionally land on ``llm_ms=1`` on a loaded VM and
    cause heuristic results to masquerade as real LLM output.
    """
    lower = content.lower()
    st = "active"
    if any(
        kw in lower for kw in ("decided", "chose", "going with", "approved", "we will")
    ):
        mt, w = "decision", 0.85
    elif any(
        kw in lower for kw in ("prefers", "likes", "always wants", "rather", "favorite")
    ):
        mt, w = "preference", 0.70
    elif any(
        kw in lower
        for kw in (
            "always notify",
            "always do",
            "never do",
            "never use",
            "never store",
            "never commit",
            "do not ever",
            "must always",
            "should always",
            "policy:",
            "guideline:",
            "rule:",
            "whenever you ",
            "whenever a ",
            "constraint:",
            "requirement:",
            "mandatory",
        )
    ):
        mt, w = "rule", 0.85
    elif any(
        kw in lower
        for kw in ("deployed", "happened", "launched", "met with", "incident")
    ):
        mt, w = "episode", 0.65
    elif any(kw in lower for kw in ("task", "todo", "assigned", "need to", "must")):
        mt, w, st = "task", 0.70, "pending"
    elif any(kw in lower for kw in ("plan", "steps", "roadmap", "strategy")):
        mt, w, st = "plan", 0.75, "pending"
    elif any(kw in lower for kw in ("commit", "promise", "guarantee", "agreed to")):
        mt, w, st = "commitment", 0.80, "pending"
    elif any(kw in lower for kw in ("cancelled", "abandoned", "stopped", "withdrew")):
        mt, w = "cancellation", 0.65
    elif any(kw in lower for kw in ("result", "outcome", "achieved", "completed")):
        mt, w, st = "outcome", 0.70, "confirmed"
    elif any(kw in lower for kw in ("intend", "aim", "goal", "want to")):
        mt, w = "intention", 0.65
    elif any(kw in lower for kw in ("did", "executed", "performed", "ran")):
        mt, w = "action", 0.65
    else:
        mt, w = "fact", 0.70

    words = content.split()
    title = " ".join(words[:10]) + ("..." if len(words) > 10 else "")

    return EnrichmentResult(
        memory_type=mt,
        weight=w,
        title=title,
        summary=content[:200],
        tags=[],
        status=st,
        llm_ms=0,  # see docstring — heuristic path, no LLM call
    )


def _demote_reserved_enrichment(result: EnrichmentResult) -> EnrichmentResult:
    """CAURA-699 — the auto-classifier must never mint a server-reserved type.

    ``insight`` / ``outcome`` / ``rule`` reach storage only via internal flows
    (insights_service / evolve_service) that set ``memory_type`` explicitly.
    The LLM path is already guarded in ``_validate_enrichment`` (the prompt
    omits these types and any hallucinated value is demoted); this is the
    matching guard for the keyword-heuristic fallback (:func:`fake_enrich`),
    whose primitive vocabulary still recognises rule/outcome shapes. Demoting
    here, rather than in the primitive, keeps ``fake_enrich`` reusable.
    """
    if result.memory_type in SERVER_RESERVED_MEMORY_TYPES:
        return result.model_copy(
            update={"memory_type": MemoryType(DEFAULT_MEMORY_TYPE)}
        )
    return result


async def enrich_memory(
    content: str,
    tenant_config: object | None = None,
    *,
    reference_datetime: datetime | None = None,
) -> EnrichmentResult:
    """Enrich memory content using LLM: classify, title, summarize, tag.

    Fallback chain:
      1. Configured provider (with retry)
      2. Alternative LLM provider (with retry) — if API key available
      3. Keyword heuristic (:func:`fake_enrich`) — always succeeds

    Uses ``tenant_config`` if provided; otherwise reads
    ``ENTITY_EXTRACTION_PROVIDER`` from the env (default ``"openai"``).
    Never raises; always returns an :class:`EnrichmentResult`.
    """
    if tenant_config is not None:
        provider_name = (
            getattr(tenant_config, "enrichment_provider", None) or ProviderName.OPENAI
        )
    else:
        provider_name = os.environ.get(
            "ENTITY_EXTRACTION_PROVIDER", ProviderName.OPENAI
        )

    if provider_name == ProviderName.FAKE:
        return _demote_reserved_enrichment(fake_enrich(content))
    if provider_name == ProviderName.NONE:
        return EnrichmentResult()

    ref_date = (
        reference_datetime.date() if reference_datetime else date.today()
    ).isoformat()

    async def _do_enrich(llm: LLMProvider) -> EnrichmentResult:
        # Escape ``{`` / ``}`` in user content before ``.format()`` —
        # the prompt template uses them as field markers, so a memory
        # whose content includes ``"I used {memory_type} in my code"``
        # would raise ``KeyError`` from ``.format()``, get caught by
        # the retry loop above, exhaust attempts, and silently fall to
        # ``fake_enrich`` with no log surface for *why*. The template
        # itself escapes its JSON example with literal ``{{ }}`` so
        # there's no symmetry to preserve at the format-string layer
        # — only user input needs the escape.
        escaped_content = content.replace("{", "{{").replace("}", "}}")
        prompt = ENRICHMENT_PROMPT.format(content=escaped_content, today=ref_date)
        t0 = time.perf_counter()
        raw = await llm.complete_json(prompt)
        llm_ms = int((time.perf_counter() - t0) * 1000)
        return _validate_enrichment(raw, llm_ms)

    enrichment_model = (
        getattr(tenant_config, "enrichment_model", None) if tenant_config else None
    )
    return await call_with_fallback(
        primary_provider_name=provider_name,
        call_fn=_do_enrich,
        fake_fn=lambda: _demote_reserved_enrichment(fake_enrich(content)),
        tenant_config=tenant_config,
        service_label="enrichment",
        model_override=enrichment_model,
    )
