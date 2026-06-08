"""Enrichment Pydantic schemas — moved from
``core_api.services.memory_enrichment`` (CAURA-595).

* :class:`AtomicFact` — one self-contained claim broken out of multi-fact
  content.
* :class:`EnrichmentResult` — the full validated enrichment payload the
  service returns. Fields mirror the JSON the LLM is prompted to
  produce in :data:`common.enrichment._prompts.ENRICHMENT_PROMPT`.

Renamed from the legacy ``MemoryEnrichment`` class (kept as a re-export
in ``core_api.services.memory_enrichment``) so that the type's role in
the new event-bus payloads is unambiguous: it's the *result* of
enrichment, distinct from the ``MemoryEnrichRequest`` event payload.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, field_validator

from common.enrichment.constants import MemoryType


# A8 — number of tags retained after normalisation. Mirrors the cap
# the prompt asks the LLM to respect; the validator enforces it
# regardless of LLM output.
_TAGS_MAX = 5

# Multi-character whitespace / underscore runs collapse to a single
# hyphen so ``code review`` / ``code  review`` / ``code_review`` /
# ``code__review`` all normalise to ``code-review``.
_TAG_SEPARATOR_RE = re.compile(r"[\s_]+")


def _normalize_tag(raw: object) -> str:
    """Apply the A8 tag normalisation: coerce-to-str, lowercase,
    collapse internal whitespace / underscores to a single hyphen,
    strip leading/trailing hyphens. Returns the normalised tag, or
    the empty string if nothing remains (caller drops empties)."""
    if raw is None:
        return ""
    s = str(raw).strip().lower()
    if not s:
        return ""
    s = _TAG_SEPARATOR_RE.sub("-", s)
    return s.strip("-")


class AtomicFact(BaseModel):
    """One atomic fact extracted from a multi-fact turn.

    Populated by the enricher when a single piece of content carries 2+
    distinct claims that would each be searched by different queries.
    Each fact becomes its own child memory with its own embedding,
    title-less but hint-enriched.
    """

    content: str
    suggested_type: MemoryType = MemoryType.FACT
    retrieval_hint: str = ""


class EnrichmentResult(BaseModel):
    """Validated LLM enrichment output.

    Mirrors the JSON schema in :data:`ENRICHMENT_PROMPT` field-for-field.
    ``llm_ms`` is filled in by the service after the LLM call returns;
    everything else is populated from the LLM's JSON response (with
    defaults for the heuristic fallback path).
    """

    memory_type: MemoryType = MemoryType.FACT
    weight: float = 0.7
    title: str = ""
    summary: str = ""
    tags: list[str] = []
    status: str = "active"

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize_tags(cls, raw):
        """A8 — defensive normalisation against LLM drift.

        Lowercases, collapses whitespace / underscores to hyphens,
        dedupes (preserves first-seen order), drops empties, caps at
        ``_TAGS_MAX`` (5). Runs even when the LLM ignored the prompt's
        format guidance, so downstream tag-joins see stable keys.
        """
        if not isinstance(raw, list):
            return []
        seen: set[str] = set()
        normalised: list[str] = []
        for item in raw:
            tag = _normalize_tag(item)
            if not tag or tag in seen:
                continue
            seen.add(tag)
            normalised.append(tag)
            if len(normalised) >= _TAGS_MAX:
                break
        return normalised

    ts_valid_start: str | None = None
    ts_valid_end: str | None = None
    contains_pii: bool = False
    pii_types: list[str] = []
    retrieval_hint: str = ""
    atomic_facts: list[AtomicFact] | None = None
    llm_ms: int = 0
