"""Tests for the ts_valid_end hygiene rules in memory_enrichment.

Covers Option B of the currency redesign:
  - Episodes never carry ts_valid_end (stripped by the validator).
  - Non-episode types (fact, intention, commitment, …) preserve LLM-provided
    ts_valid_end so explicit deadlines / expiries still work.
  - The enrichment prompt contains the episode rule.
"""

from __future__ import annotations

from core_api.services.memory_enrichment import (
    ENRICHMENT_PROMPT,
    _validate_enrichment,
)


# ---------------------------------------------------------------------------
# Validator: episode strips ts_valid_end
# ---------------------------------------------------------------------------


class TestEpisodeStripsTsValidEnd:
    def test_episode_with_llm_end_date_is_stripped(self):
        """Simulates the Petra failure: LLM set ts_valid_end on an episode."""
        raw = {
            "memory_type": "episode",
            "weight": 0.7,
            "title": "Interest in Petra history (learned via lecture)",
            "summary": "User learned about Petra in a lecture this month.",
            "tags": ["petra", "history"],
            "status": "active",
            "ts_valid_start": "2023-01-11T06:16:00+00:00",
            "ts_valid_end": "2023-01-31T23:59:59+00:00",
            "contains_pii": False,
            "pii_types": [],
        }
        out = _validate_enrichment(raw, llm_ms=0)
        assert out.memory_type == "episode"
        assert out.ts_valid_start == "2023-01-11T06:16:00+00:00"
        assert out.ts_valid_end is None  # stripped

    def test_episode_with_null_end_date_stays_null(self):
        raw = {
            "memory_type": "episode",
            "weight": 0.7,
            "title": "Attended conference",
            "summary": "User went to a conference.",
            "tags": [],
            "status": "active",
            "ts_valid_start": "2023-05-10T09:00:00+00:00",
            "ts_valid_end": None,
            "contains_pii": False,
            "pii_types": [],
        }
        out = _validate_enrichment(raw, llm_ms=0)
        assert out.ts_valid_end is None


# ---------------------------------------------------------------------------
# Validator: non-episode types preserve ts_valid_end
# ---------------------------------------------------------------------------


class TestNonEpisodePreservesTsValidEnd:
    def test_fact_preserves_end(self):
        """Explicit expiry on a fact ("contract expires …") must survive."""
        raw = {
            "memory_type": "fact",
            "weight": 0.8,
            "title": "Freelance contract with client A",
            "summary": "Contract signed, expires end of 2024.",
            "tags": ["contract"],
            "status": "active",
            "ts_valid_start": "2024-01-01T00:00:00+00:00",
            "ts_valid_end": "2024-12-31T23:59:59+00:00",
            "contains_pii": False,
            "pii_types": [],
        }
        out = _validate_enrichment(raw, llm_ms=0)
        assert out.memory_type == "fact"
        assert out.ts_valid_end == "2024-12-31T23:59:59+00:00"

    def test_intention_preserves_end(self):
        raw = {
            "memory_type": "intention",
            "weight": 0.7,
            "title": "Plan to ship v2 by July",
            "summary": "User intends to release v2 before 2024-07-01.",
            "tags": ["plan"],
            "status": "pending",
            "ts_valid_start": "2024-03-01T00:00:00+00:00",
            "ts_valid_end": "2024-07-01T00:00:00+00:00",
            "contains_pii": False,
            "pii_types": [],
        }
        out = _validate_enrichment(raw, llm_ms=0)
        assert out.memory_type == "intention"
        assert out.ts_valid_end == "2024-07-01T00:00:00+00:00"

    def test_commitment_preserves_end(self):
        raw = {
            "memory_type": "commitment",
            "weight": 0.9,
            "title": "Promised weekly status reports",
            "summary": "User committed to sending reports through Q2.",
            "tags": ["commitment"],
            "status": "pending",
            "ts_valid_start": "2024-01-01T00:00:00+00:00",
            "ts_valid_end": "2024-06-30T23:59:59+00:00",
            "contains_pii": False,
            "pii_types": [],
        }
        out = _validate_enrichment(raw, llm_ms=0)
        assert out.ts_valid_end == "2024-06-30T23:59:59+00:00"


# ---------------------------------------------------------------------------
# Prompt: the new episode rule is actually present
# ---------------------------------------------------------------------------


class TestPromptContainsEpisodeRule:
    def test_prompt_forbids_episode_ts_valid_end(self):
        """The prompt must explicitly tell the LLM not to set end dates on episodes."""
        assert "episode" in ENRICHMENT_PROMPT.lower()
        # Look for the specific guidance
        assert "DO NOT set ts_valid_end for memory_type \"episode\"" in ENRICHMENT_PROMPT

    def test_prompt_forbids_relative_modifier_inference(self):
        """Specific anti-pattern: relative modifiers like 'this month' are a trap."""
        assert "this month" in ENRICHMENT_PROMPT
        assert "DO NOT infer ts_valid_end from relative time modifiers" in ENRICHMENT_PROMPT
