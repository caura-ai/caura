"""CAURA-699: the auto-classifier must never mint a server-reserved type.

``insight`` / ``outcome`` / ``rule`` are authored only by internal flows
(``insights_service`` for insight, ``evolve_service`` for outcome/rule) that
set ``memory_type`` explicitly. External agent writes flow through the
enrichment auto-classifier, which previously could classify content as one of
these — polluting, in particular, the insight space that the reports flow
assumes is server-owned.

Two guards enforce the policy:
  1. The enrichment prompt omits the reserved types from the offered
     vocabulary (see ``tests/test_enrichment_prompt.py``).
  2. Any reserved type that still surfaces is demoted to ``DEFAULT_MEMORY_TYPE``
     — in ``_validate_enrichment`` for the LLM path (top-level type AND each
     atomic-fact ``suggested_type``), and at the ``enrich_memory`` boundary for
     the keyword-heuristic fallback.

These tests exercise the demotion directly; the boundary-rejection of
*explicit* agent-supplied reserved types is covered by
``tests/test_c3_c8_reserved_memory_types.py``.
"""

import pytest

from common.enrichment.constants import (
    DEFAULT_MEMORY_TYPE,
    SERVER_RESERVED_MEMORY_TYPES,
)
from common.enrichment.service import (
    _validate_enrichment,
    enrich_memory,
    fake_enrich,
)


class _FakeProviderConfig:
    """Minimal tenant_config routing enrich_memory to the keyword heuristic."""

    enrichment_provider = "fake"
    enrichment_enabled = True
    enrichment_model = None


@pytest.mark.unit
class TestLLMPathDemotion:
    @pytest.mark.parametrize("reserved", sorted(SERVER_RESERVED_MEMORY_TYPES))
    def test_reserved_top_level_type_demoted(self, reserved):
        """A reserved type hallucinated by the LLM is demoted to the default."""
        result = _validate_enrichment(
            {"memory_type": reserved, "weight": 0.9, "title": "t"}, llm_ms=5
        )
        assert result.memory_type == DEFAULT_MEMORY_TYPE

    @pytest.mark.parametrize("reserved", sorted(SERVER_RESERVED_MEMORY_TYPES))
    def test_reserved_atomic_fact_suggested_type_demoted(self, reserved):
        """A reserved ``suggested_type`` on an atomic fact is demoted too."""
        result = _validate_enrichment(
            {
                "memory_type": "fact",
                "title": "t",
                "atomic_facts": [
                    {"content": "a distinct claim", "suggested_type": reserved},
                    {"content": "another distinct claim", "suggested_type": "decision"},
                ],
            },
            llm_ms=5,
        )
        types = [f.suggested_type for f in result.atomic_facts]
        assert types[0] == DEFAULT_MEMORY_TYPE
        assert types[1] == "decision"  # non-reserved type preserved

    def test_non_reserved_type_preserved(self):
        result = _validate_enrichment(
            {"memory_type": "decision", "weight": 0.8, "title": "t"}, llm_ms=5
        )
        assert result.memory_type == "decision"

    def test_unknown_type_still_falls_to_default(self):
        result = _validate_enrichment(
            {"memory_type": "not-a-real-type", "title": "t"}, llm_ms=5
        )
        assert result.memory_type == DEFAULT_MEMORY_TYPE


@pytest.mark.unit
class TestHeuristicBoundaryDemotion:
    def test_primitive_still_recognises_reserved_shapes(self):
        """The reusable primitive is intentionally unchanged — it still
        classifies rule/outcome shapes. The policy is enforced one level up,
        at the ``enrich_memory`` boundary."""
        assert (
            fake_enrich("Always notify security before deploying").memory_type == "rule"
        )
        assert (
            fake_enrich("The migration achieved a great result").memory_type
            == "outcome"
        )

    @pytest.mark.asyncio
    async def test_enrich_memory_demotes_rule_shaped_heuristic(self):
        result = await enrich_memory(
            "Always notify security before deploying", _FakeProviderConfig()
        )
        assert result.memory_type == DEFAULT_MEMORY_TYPE

    @pytest.mark.asyncio
    async def test_enrich_memory_demotes_outcome_shaped_heuristic(self):
        result = await enrich_memory(
            "The migration achieved a great result", _FakeProviderConfig()
        )
        assert result.memory_type == DEFAULT_MEMORY_TYPE

    @pytest.mark.asyncio
    async def test_enrich_memory_preserves_non_reserved_heuristic(self):
        result = await enrich_memory(
            "We decided to use PostgreSQL", _FakeProviderConfig()
        )
        assert result.memory_type == "decision"
