"""Tests for the atomic_facts feature in memory_enrichment.

Covers the new multi-fact decomposition field:
  - Validator parses + sanitizes atomic_facts; drops malformed entries.
  - Single-fact output is collapsed to None (no fan-out needed).
  - Prompt contains the multi-fact rule + concrete examples.
  - AtomicFact model round-trips through MemoryEnrichment.
"""

from __future__ import annotations

from core_api.services.memory_enrichment import (
    ENRICHMENT_PROMPT,
    AtomicFact,
    MemoryEnrichment,
    _validate_enrichment,
)


def _raw(**overrides):
    base = {
        "memory_type": "episode",
        "weight": 0.7,
        "title": "Multi-topic turn",
        "summary": "Covers two unrelated facts.",
        "tags": [],
        "status": "active",
        "ts_valid_start": None,
        "ts_valid_end": None,
        "contains_pii": False,
        "pii_types": [],
        "retrieval_hint": "",
        "atomic_facts": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Validator: atomic_facts happy path
# ---------------------------------------------------------------------------


class TestValidatorAtomicFactsParsing:
    def test_two_valid_facts_round_trip(self):
        raw = _raw(
            atomic_facts=[
                {
                    "content": "Rachel got engaged on May 15th",
                    "suggested_type": "episode",
                    "retrieval_hint": "Rachel engagement date",
                },
                {
                    "content": "Looking for gift ideas for sister-in-law's sister",
                    "suggested_type": "intention",
                    "retrieval_hint": "gift planning for sister-in-law",
                },
            ],
        )
        out = _validate_enrichment(raw, llm_ms=0)
        assert out.atomic_facts is not None
        assert len(out.atomic_facts) == 2
        assert out.atomic_facts[0].content == "Rachel got engaged on May 15th"
        assert out.atomic_facts[0].suggested_type == "episode"
        assert out.atomic_facts[0].retrieval_hint == "Rachel engagement date"

    def test_three_facts_all_round_trip(self):
        raw = _raw(
            atomic_facts=[
                {"content": "A", "suggested_type": "fact", "retrieval_hint": "a"},
                {"content": "B", "suggested_type": "fact", "retrieval_hint": "b"},
                {"content": "C", "suggested_type": "fact", "retrieval_hint": "c"},
            ],
        )
        out = _validate_enrichment(raw, llm_ms=0)
        assert len(out.atomic_facts) == 3


# ---------------------------------------------------------------------------
# Validator: single-fact collapse
# ---------------------------------------------------------------------------


class TestValidatorEdgeCases:
    def test_single_fact_is_preserved(self):
        """A single orphan fact the LLM flagged still gets its own child."""
        raw = _raw(
            atomic_facts=[
                {"content": "Only one orphan fact", "suggested_type": "fact"},
            ],
        )
        out = _validate_enrichment(raw, llm_ms=0)
        assert out.atomic_facts is not None
        assert len(out.atomic_facts) == 1
        assert out.atomic_facts[0].content == "Only one orphan fact"

    def test_empty_list_returns_none(self):
        out = _validate_enrichment(_raw(atomic_facts=[]), llm_ms=0)
        assert out.atomic_facts is None

    def test_missing_field_returns_none(self):
        r = _raw()
        r.pop("atomic_facts", None)
        out = _validate_enrichment(r, llm_ms=0)
        assert out.atomic_facts is None


# ---------------------------------------------------------------------------
# Validator: sanitization of malformed entries
# ---------------------------------------------------------------------------


class TestValidatorSanitization:
    def test_drops_non_dict_entries(self):
        raw = _raw(
            atomic_facts=[
                "just a string",  # invalid
                {"content": "valid fact A", "suggested_type": "fact"},
                123,  # invalid
                {"content": "valid fact B", "suggested_type": "episode"},
            ],
        )
        out = _validate_enrichment(raw, llm_ms=0)
        assert len(out.atomic_facts) == 2
        assert [f.content for f in out.atomic_facts] == ["valid fact A", "valid fact B"]

    def test_drops_entries_missing_content(self):
        raw = _raw(
            atomic_facts=[
                {"suggested_type": "fact"},  # no content
                {"content": "has content", "suggested_type": "fact"},
                {"content": "", "suggested_type": "fact"},  # empty content
                {"content": "   ", "suggested_type": "fact"},  # whitespace-only
                {"content": "another valid one", "suggested_type": "episode"},
            ],
        )
        out = _validate_enrichment(raw, llm_ms=0)
        assert len(out.atomic_facts) == 2
        assert [f.content for f in out.atomic_facts] == ["has content", "another valid one"]

    def test_invalid_suggested_type_falls_back_to_fact(self):
        raw = _raw(
            atomic_facts=[
                {"content": "one", "suggested_type": "not-a-real-type"},
                {"content": "two", "suggested_type": "also-bogus"},
            ],
        )
        out = _validate_enrichment(raw, llm_ms=0)
        assert all(f.suggested_type == "fact" for f in out.atomic_facts)

    def test_missing_retrieval_hint_defaults_to_empty(self):
        raw = _raw(
            atomic_facts=[
                {"content": "no hint", "suggested_type": "fact"},
                {"content": "has hint", "suggested_type": "fact", "retrieval_hint": "x"},
            ],
        )
        out = _validate_enrichment(raw, llm_ms=0)
        assert out.atomic_facts[0].retrieval_hint == ""
        assert out.atomic_facts[1].retrieval_hint == "x"

    def test_content_trimmed(self):
        raw = _raw(
            atomic_facts=[
                {"content": "  leading/trailing  ", "suggested_type": "fact"},
                {"content": "another one", "suggested_type": "fact"},
            ],
        )
        out = _validate_enrichment(raw, llm_ms=0)
        assert out.atomic_facts[0].content == "leading/trailing"


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


class TestPromptContainsAtomicFactsRule:
    def test_prompt_has_atomic_facts_field(self):
        assert '"atomic_facts"' in ENRICHMENT_PROMPT

    def test_prompt_has_rachel_example(self):
        # The canonical multi-fact example (modeled on the 6613b389 case).
        assert "Rachel got engaged" in ENRICHMENT_PROMPT

    def test_prompt_tells_llm_default_null(self):
        assert "null in almost all cases" in ENRICHMENT_PROMPT

    def test_prompt_discourages_single_topic_splitting(self):
        assert "DO NOT fan out single-topic content" in ENRICHMENT_PROMPT


# ---------------------------------------------------------------------------
# Model round-trip
# ---------------------------------------------------------------------------


class TestMemoryEnrichmentModel:
    def test_atomic_fact_model_defaults(self):
        fact = AtomicFact(content="example")
        assert fact.suggested_type == "fact"
        assert fact.retrieval_hint == ""

    def test_memory_enrichment_default_atomic_facts_none(self):
        enrichment = MemoryEnrichment()
        assert enrichment.atomic_facts is None

    def test_memory_enrichment_carries_list(self):
        enrichment = MemoryEnrichment(
            atomic_facts=[AtomicFact(content="a"), AtomicFact(content="b")]
        )
        assert enrichment.atomic_facts is not None
        assert len(enrichment.atomic_facts) == 2
