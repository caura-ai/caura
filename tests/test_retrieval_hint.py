"""Tests for the retrieval_hint metadata field.

Covers:
  - Validator round-trips retrieval_hint; trims whitespace; caps length.
  - Prompt contains the retrieval_hint rule + concrete examples.

The hint is persisted in metadata (debugging / auditability) but no
longer participates in embedding composition — see CAURA-222 and the
follow-up that removed ``compose_embedding_text`` entirely.
"""

from __future__ import annotations

from core_api.services.memory_enrichment import (
    ENRICHMENT_PROMPT,
    _validate_enrichment,
)


# ---------------------------------------------------------------------------
# Validator: retrieval_hint handling
# ---------------------------------------------------------------------------


class TestValidatorRetrievalHint:
    def _raw(self, **overrides):
        base = {
            "memory_type": "episode",
            "weight": 0.7,
            "title": "Signed first client",
            "summary": "User signed a contract with their first paying client.",
            "tags": [],
            "status": "active",
            "ts_valid_start": None,
            "ts_valid_end": None,
            "contains_pii": False,
            "pii_types": [],
            "retrieval_hint": "",
        }
        base.update(overrides)
        return base

    def test_hint_round_trips(self):
        out = _validate_enrichment(
            self._raw(retrieval_hint="business milestone: first client signed"),
            llm_ms=0,
        )
        assert out.retrieval_hint == "business milestone: first client signed"

    def test_hint_whitespace_trimmed(self):
        out = _validate_enrichment(
            self._raw(retrieval_hint="   museum visit lecture   "),
            llm_ms=0,
        )
        assert out.retrieval_hint == "museum visit lecture"

    def test_hint_length_capped_at_200(self):
        long_hint = "x" * 500
        out = _validate_enrichment(
            self._raw(retrieval_hint=long_hint),
            llm_ms=0,
        )
        assert len(out.retrieval_hint) == 200

    def test_missing_hint_defaults_to_empty(self):
        raw = self._raw()
        raw.pop("retrieval_hint", None)
        out = _validate_enrichment(raw, llm_ms=0)
        assert out.retrieval_hint == ""

    def test_non_string_hint_coerced_to_empty(self):
        out = _validate_enrichment(
            self._raw(retrieval_hint=123),  # wrong type from bad LLM output
            llm_ms=0,
        )
        assert out.retrieval_hint == ""


# ---------------------------------------------------------------------------
# Prompt: new rule is present and anchored with examples
# ---------------------------------------------------------------------------


class TestPromptContainsHintRule:
    def test_prompt_has_retrieval_hint_field(self):
        assert '"retrieval_hint"' in ENRICHMENT_PROMPT

    def test_prompt_describes_purpose(self):
        # Key phrase from the rule so drift in the prompt is obvious
        assert "SEMANTIC ESSENCE" in ENRICHMENT_PROMPT

    def test_prompt_includes_business_milestone_example(self):
        # This is the example modeled after the eac54add failure case.
        assert "business milestone" in ENRICHMENT_PROMPT
