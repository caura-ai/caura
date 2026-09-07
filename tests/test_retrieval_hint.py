"""Tests for the retrieval_hint metadata field.

Covers:
  - Validator round-trips retrieval_hint; trims whitespace; caps length.
  - The prompt no longer ASKS for it (CAURA-720).

The hint never participates in embedding composition — CAURA-222
disabled that and the follow-up removed ``compose_embedding_text``
entirely. CAURA-720 then retired the prompt field, since the only
remaining reader is ``scripts/backfill_embeddings.py``, which selects on
a non-empty ``metadata.retrieval_hint`` to find rows whose stored vector
still holds the old prefixed text.

The validator tests stay: historical rows carry hint values, and the
deferred worker replays stored enrichment payloads.
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
# Prompt: the rule is RETIRED (CAURA-720)
# ---------------------------------------------------------------------------


class TestPromptNoLongerAsksForHint:
    """CAURA-720 removed the field from the prompt.

    The rule existed to augment the embedding, and CAURA-222 disabled that
    — writes embedded ``"[Retrieval hint]: …\\n\\n<content>"`` while queries
    embedded raw text, so identical content↔query scored cosine ~0.69
    instead of ~1.0. With no consumer left, the prompt stopped asking.

    The validator section above still applies: historical rows and the
    deferred worker's replay path both still carry hint values.
    """

    def test_prompt_does_not_request_retrieval_hint(self):
        assert '"retrieval_hint"' not in ENRICHMENT_PROMPT

    def test_hint_guidance_is_gone(self):
        # The phrases that only ever existed to shape hint output.
        for phrase in (
            "SEMANTIC ESSENCE",
            "business milestone",
            "WHY-THIS-IS-NOTEWORTHY",
        ):
            assert phrase not in ENRICHMENT_PROMPT, (
                f"leftover hint guidance: {phrase!r}"
            )

    def test_atomic_facts_no_longer_carries_a_per_fact_hint(self):
        """The sub-schema listed its own ``retrieval_hint`` per fact. Children
        embed raw ``fact_content`` for the same CAURA-222 reason, so that copy
        was equally inert."""
        facts_block = ENRICHMENT_PROMPT.split('"atomic_facts"', 1)[-1]
        assert "retrieval_hint" not in facts_block
