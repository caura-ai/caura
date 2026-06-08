"""A1 #16 — LLM judge for the dedup danger zone.

Two-tier dedup (see ``common.constants.SEMANTIC_DEDUP_AUTO_THRESHOLD``
and ``SEMANTIC_DEDUP_JUDGE_THRESHOLD``, both added in A1 #15):

  similarity ≥ AUTO   → auto-reject 409 (no LLM)
  JUDGE ≤ sim < AUTO  → call ``_llm_dedup_check`` to decide
  similarity < JUDGE  → accept (no candidate surfaced)

This module owns the judge. ``CheckSemanticDuplicate`` (the pipeline
step) imports ``_llm_dedup_check`` and gates the 409 on its return.

The judge returns ``(is_duplicate, confidence)`` mirroring A4 #12's
``_llm_contradiction_check``. Confidence rubric is simpler here — the
prompt has no safety gates (no subject extraction, no non-conflict
reason taxonomy), so:

  0.90 — Clean response (``is_duplicate`` is a bool, ``reason`` is a
         non-empty string).
  0.50 — Malformed / unparseable / heuristic fallback.

Callers gate 409 on a configurable confidence threshold so a flaky
LLM response doesn't 409 a legitimate write.
"""

from __future__ import annotations

import logging

from core_api.config import settings
from core_api.providers._retry import call_with_fallback

logger = logging.getLogger(__name__)


# Confidence rubric. See module docstring for the rationale.
_CONF_CLEAN = 0.90
_CONF_FALLBACK = 0.50


DEDUP_PROMPT = """\
You are a deduplication judge for a personal memory system.

Two short statements are about to be stored. Decide whether they
encode the SAME memory (i.e. one is a duplicate or trivial paraphrase
of the other) or DIFFERENT memories (even if they overlap in topic).

Statement A (NEW): {new_content}
Statement B (EXISTING): {old_content}

Return STRICT JSON with no surrounding prose:
{{
  "is_duplicate": true|false,
  "reason": "<one short clause>"
}}

A is a duplicate of B iff a reasonable reader would consider the two
interchangeable carriers of the same fact. Refinements that ADD new
information are NOT duplicates. Different subjects are NOT duplicates
even if their predicates match.
"""


def _judge_dedup(raw) -> tuple[bool, float]:
    """Parse the LLM response into ``(is_duplicate, confidence)``.

    Conservative defaults: anything that doesn't look like a well-formed
    ``{is_duplicate: bool, reason: str}`` dict → ``(False, 0.50)``.
    Downstream callers gate 409 on confidence, so this floor means a
    flaky response will not 409 a legitimate write.
    """
    if not isinstance(raw, dict):
        return False, _CONF_FALLBACK

    is_dup_raw = raw.get("is_duplicate")
    reason_raw = raw.get("reason")
    if not isinstance(is_dup_raw, bool):
        return False, _CONF_FALLBACK
    if not isinstance(reason_raw, str) or not reason_raw.strip():
        return False, _CONF_FALLBACK
    return is_dup_raw, _CONF_CLEAN


def _fake_dedup_check(new_content: str, old_content: str) -> bool:
    """Heuristic fallback when no LLM credentials are available.

    Conservative: only flags as duplicate when the two strings are
    near-identical after whitespace normalisation. Real dedup judgement
    is best deferred to the LLM; this exists so the pipeline doesn't
    raise on test/CI environments without credentials.
    """
    return " ".join(new_content.split()).lower() == " ".join(old_content.split()).lower()


async def _llm_dedup_check(
    new_content: str,
    old_content: str,
    tenant_config=None,
) -> tuple[bool, float]:
    """Ask the LLM whether two texts are duplicates.

    Returns ``(is_duplicate, confidence)`` — see ``_judge_dedup`` for
    the rubric. Uses the same 3-tier fallback chain as
    ``_llm_contradiction_check``: configured provider → fallback
    provider → heuristic.
    """
    provider_name = (
        tenant_config.entity_extraction_provider if tenant_config else settings.entity_extraction_provider
    )

    prompt = DEDUP_PROMPT.format(new_content=new_content[:500], old_content=old_content[:500])

    async def _do_check(llm) -> tuple[bool, float]:
        raw = await llm.complete_json(prompt)
        return _judge_dedup(raw)

    return await call_with_fallback(
        primary_provider_name=provider_name,
        call_fn=_do_check,
        fake_fn=lambda: (_fake_dedup_check(new_content, old_content), _CONF_FALLBACK),
        tenant_config=tenant_config,
        service_label="dedup",
        model_attr="entity_extraction_model",
        timeout=10.0,
    )


# Below this, callers must NOT 409 — even if ``is_duplicate=True``.
# Set at gate-1 / gate-2 confidence floor so heuristic fallbacks and
# malformed responses don't sink legitimate writes.
DEDUP_JUDGE_CONFIDENCE_THRESHOLD = 0.60
