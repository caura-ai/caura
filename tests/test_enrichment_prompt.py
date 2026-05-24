"""P2-6: Enrichment prompt signal phrases for better type classification.

Unit tests validate:
- Prompt text includes signal phrases for decision, commitment, rule
- Signal phrases don't inflate prompt token count excessively
- Keyword heuristic still correctly classifies all types (regression)
"""

import pytest


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnrichmentPromptSignalPhrases:
    """Verify signal phrases are present in the enrichment prompt."""

    def _get_prompt(self) -> str:
        from core_api.services.memory_enrichment import ENRICHMENT_PROMPT

        return ENRICHMENT_PROMPT

    def test_decision_has_signal_phrases(self):
        prompt = self._get_prompt()
        assert "decided to" in prompt
        assert "going with" in prompt
        assert "opted for" in prompt

    def test_commitment_has_signal_phrases(self):
        prompt = self._get_prompt()
        assert "committed to" in prompt
        assert "pledged" in prompt

    def test_rule_has_signal_phrases(self):
        prompt = self._get_prompt()
        # Rule line should mention key directive words
        assert "always" in prompt.lower()
        assert "never" in prompt.lower()

    def test_prompt_token_count_reasonable(self):
        """Prompt ceiling — keeps cost bounded while leaving room for small additions.

        Baseline grew with retrieval_hint, atomic_facts, the temporal
        ts_valid_end guidance, A8's tag-format guidance, and A9's
        action/episode disambiguation pairs. Raised to 1200 (2026-05-24).
        """
        prompt = self._get_prompt()
        word_count = len(prompt.split())
        assert word_count < 1200, (
            f"Prompt is {word_count} words — too long, will increase cost"
        )

    def test_prompt_structure_unchanged(self):
        """JSON template should still be present and valid."""
        prompt = self._get_prompt()
        assert '"memory_type": "..."' in prompt
        assert '"weight": 0.0' in prompt
        assert '"tags": ["..."]' in prompt
        assert "Content:" in prompt


@pytest.mark.unit
class TestEnrichmentPromptVocabularySync:
    """Prompt vocabulary must stay in lock-step with ``MEMORY_TYPES``.

    Lives here because the prompt was the original drift site: ``insight``
    was added to the tuple but missed in the prompt's hardcoded list,
    causing the LLM to never emit that type. The renderer in
    ``common/enrichment/_prompts.py`` now derives both the inline list
    and the bullet descriptions from ``MEMORY_TYPE_DESCRIPTIONS``, and
    these tests guard that contract.
    """

    def _get_prompt(self) -> str:
        from core_api.services.memory_enrichment import ENRICHMENT_PROMPT

        return ENRICHMENT_PROMPT

    def test_every_memory_type_appears_quoted_in_inline_list(self):
        from common.enrichment.constants import MEMORY_TYPES

        prompt = self._get_prompt()
        for t in MEMORY_TYPES:
            assert f'"{t}"' in prompt, (
                f"memory_type {t!r} missing from inline list — prompt vocabulary "
                "drifted from MEMORY_TYPES"
            )

    def test_every_memory_type_has_a_bullet(self):
        from common.enrichment.constants import MEMORY_TYPE_DESCRIPTIONS

        prompt = self._get_prompt()
        for name, desc in MEMORY_TYPE_DESCRIPTIONS.items():
            assert f"   - {name}: " in prompt, f"{name!r} bullet missing from prompt"
            # First clause of the description should also land verbatim
            first_clause = desc.split(".")[0].split(",")[0].strip()
            assert first_clause in prompt, (
                f"{name!r} description first clause not found in prompt: {first_clause!r}"
            )

    def test_pattern_matches_every_memory_type(self):
        import re
        from common.enrichment.constants import MEMORY_TYPES
        from core_api.constants import MEMORY_TYPES_PATTERN

        for t in MEMORY_TYPES:
            assert re.match(MEMORY_TYPES_PATTERN, t), (
                f"{t!r} not accepted by MEMORY_TYPES_PATTERN — pattern drifted"
            )


@pytest.mark.unit
class TestHeuristicRegressions:
    """Ensure keyword heuristic still classifies all types correctly after prompt changes."""

    def test_decision_classification(self):
        from core_api.services.memory_enrichment import _fake_enrich

        assert _fake_enrich("We decided to use PostgreSQL").memory_type == "decision"
        assert _fake_enrich("Team chose Redis for caching").memory_type == "decision"
        assert _fake_enrich("Management approved the budget").memory_type == "decision"

    def test_preference_classification(self):
        from core_api.services.memory_enrichment import _fake_enrich

        assert _fake_enrich("The team prefers dark mode").memory_type == "preference"

    def test_episode_classification(self):
        from core_api.services.memory_enrichment import _fake_enrich

        assert _fake_enrich("We deployed v2.3 to production").memory_type == "episode"

    def test_task_classification(self):
        from core_api.services.memory_enrichment import _fake_enrich

        assert _fake_enrich("Need to review the PR by Friday").memory_type == "task"

    def test_commitment_classification(self):
        from core_api.services.memory_enrichment import _fake_enrich

        assert (
            _fake_enrich("We committed to delivering by Q2").memory_type == "commitment"
        )

    def test_rule_classification(self):
        from core_api.services.memory_enrichment import _fake_enrich

        assert (
            _fake_enrich("Always notify security before deploying").memory_type
            == "rule"
        )
        assert _fake_enrich("Never store PII in Redis").memory_type == "rule"

    def test_cancellation_classification(self):
        from core_api.services.memory_enrichment import _fake_enrich

        assert (
            _fake_enrich("The project was cancelled last week").memory_type
            == "cancellation"
        )

    def test_outcome_classification(self):
        from core_api.services.memory_enrichment import _fake_enrich

        assert (
            _fake_enrich("The migration achieved 99.9% uptime").memory_type == "outcome"
        )

    def test_fact_default(self):
        from core_api.services.memory_enrichment import _fake_enrich

        assert _fake_enrich("The server runs on port 8080").memory_type == "fact"

    def test_plan_classification(self):
        from core_api.services.memory_enrichment import _fake_enrich

        assert _fake_enrich("The roadmap includes three phases").memory_type == "plan"

    def test_intention_classification(self):
        from core_api.services.memory_enrichment import _fake_enrich

        assert (
            _fake_enrich("We intend to migrate to AWS next quarter").memory_type
            == "intention"
        )

    def test_action_classification(self):
        from core_api.services.memory_enrichment import _fake_enrich

        assert (
            _fake_enrich("I executed the database backup script").memory_type
            == "action"
        )
