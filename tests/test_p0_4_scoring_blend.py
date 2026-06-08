"""P0-4: Additive similarity/weight scoring blend.

Unit tests validate the base_score formula and ranking behavior.
"""

import pytest

from core_api.constants import SIMILARITY_BLEND


# ---------------------------------------------------------------------------
# Pure math — replicate the SQL base_score in Python
# ---------------------------------------------------------------------------

def compute_base_score(similarity: float, weight: float) -> float:
    """Python equivalent of: SIMILARITY_BLEND * similarity + (1 - SIMILARITY_BLEND) * weight."""
    return SIMILARITY_BLEND * similarity + (1.0 - SIMILARITY_BLEND) * weight


def compute_old_score(similarity: float, weight: float) -> float:
    """The OLD multiplicative formula for comparison."""
    return similarity * weight


def compute_full_score(
    similarity: float,
    weight: float,
    freshness: float = 1.0,
    entity_boost: float = 1.0,
    recall_boost: float = 1.0,
) -> float:
    """Full scoring pipeline (new formula)."""
    base = compute_base_score(similarity, weight)
    return base * freshness * entity_boost * recall_boost


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestScoringBlend:

    def test_similarity_blend_constant_valid(self):
        """SIMILARITY_BLEND must be between 0.5 and 1.0 (similarity-dominant)."""
        assert 0.5 <= SIMILARITY_BLEND <= 1.0

    # -- The core fix: weight no longer dominates similarity --

    def test_high_similarity_low_weight_beats_low_similarity_high_weight(self):
        """THE KEY FIX: relevant + unimportant should beat irrelevant + important.

        Before: 0.95 * 0.3 = 0.285 vs 0.60 * 0.9 = 0.540 → wrong winner
        After:  0.75*0.95 + 0.25*0.3 = 0.788 vs 0.75*0.6 + 0.25*0.9 = 0.675 → correct
        """
        relevant_unimportant = compute_base_score(similarity=0.95, weight=0.3)
        irrelevant_important = compute_base_score(similarity=0.60, weight=0.9)
        assert relevant_unimportant > irrelevant_important, (
            f"Relevant memory ({relevant_unimportant:.3f}) should outscore "
            f"irrelevant one ({irrelevant_important:.3f})"
        )

    def test_old_formula_had_wrong_ranking(self):
        """Confirm the old formula produced the wrong ranking (regression guard)."""
        old_relevant = compute_old_score(similarity=0.95, weight=0.3)
        old_irrelevant = compute_old_score(similarity=0.60, weight=0.9)
        assert old_relevant < old_irrelevant, (
            "Old formula should have ranked the irrelevant memory higher (the bug)"
        )

    # -- Boundary behavior --

    def test_perfect_similarity_and_weight(self):
        """Both maxed out → base_score = 1.0."""
        score = compute_base_score(1.0, 1.0)
        assert score == pytest.approx(1.0)

    def test_zero_similarity_zero_weight(self):
        """Both zero → base_score = 0.0."""
        score = compute_base_score(0.0, 0.0)
        assert score == pytest.approx(0.0)

    def test_zero_similarity_full_weight(self):
        """Zero relevance → score should be low even with max weight."""
        score = compute_base_score(similarity=0.0, weight=1.0)
        assert score == pytest.approx(1.0 - SIMILARITY_BLEND)
        assert score < 0.5, "Zero-similarity memory must not score above 0.5"

    def test_full_similarity_zero_weight(self):
        """Max relevance, min weight → score should still be high."""
        score = compute_base_score(similarity=1.0, weight=0.0)
        assert score == pytest.approx(SIMILARITY_BLEND)
        assert score > 0.5, "Max-similarity memory must score above 0.5 even with zero weight"

    # -- Ranking scenarios --

    def test_similarity_is_primary_signal(self):
        """Moderate similarity gap should overcome large weight difference."""
        # 0.30 similarity gap vs 0.6 weight gap
        a = compute_base_score(similarity=0.95, weight=0.2)
        b = compute_base_score(similarity=0.65, weight=0.8)
        # 0.75*0.95 + 0.25*0.2 = 0.7625 vs 0.75*0.65 + 0.25*0.8 = 0.6875
        assert a > b, "0.30 similarity gap should beat 0.6 weight gap"

    def test_equal_similarity_weight_decides(self):
        """When similarity is identical, weight breaks the tie."""
        a = compute_base_score(similarity=0.80, weight=0.9)
        b = compute_base_score(similarity=0.80, weight=0.3)
        assert a > b, "Equal similarity → weight should be the tiebreaker"

    def test_extreme_weight_gap_with_close_similarity(self):
        """Even with weight=1.0 vs weight=0.0, a 0.4 similarity lead wins."""
        a = compute_base_score(similarity=0.95, weight=0.0)
        b = compute_base_score(similarity=0.55, weight=1.0)
        assert a > b

    # -- Full pipeline scoring --

    def test_freshness_modifier_applied_correctly(self):
        """Freshness multiplies the base_score."""
        fresh = compute_full_score(similarity=0.8, weight=0.5, freshness=1.0)
        stale = compute_full_score(similarity=0.8, weight=0.5, freshness=0.5)
        assert fresh == pytest.approx(stale * 2, abs=0.01)

    def test_entity_boost_applied_correctly(self):
        """Entity boost multiplies correctly."""
        base = compute_full_score(similarity=0.8, weight=0.5, entity_boost=1.0)
        boosted = compute_full_score(similarity=0.8, weight=0.5, entity_boost=1.3)
        assert boosted == pytest.approx(base * 1.3, abs=0.01)

    def test_all_modifiers_compound(self):
        """All modifiers multiply together on the base_score."""
        base = compute_base_score(0.8, 0.6)
        full = compute_full_score(0.8, 0.6, freshness=0.9, entity_boost=1.2, recall_boost=1.1)
        expected = base * 0.9 * 1.2 * 1.1
        assert full == pytest.approx(expected, abs=0.001)

    # -- Ranking stability across the formula --

    def test_realistic_ranking_scenario(self):
        """A realistic set of 5 memories ranked correctly."""
        memories = [
            {"sim": 0.95, "w": 0.3, "fresh": 1.0, "label": "highly relevant, low weight"},
            {"sim": 0.60, "w": 0.9, "fresh": 1.0, "label": "moderate relevance, high weight"},
            {"sim": 0.92, "w": 0.8, "fresh": 1.0, "label": "very relevant, high weight"},
            {"sim": 0.85, "w": 0.5, "fresh": 0.5, "label": "relevant but stale"},
            {"sim": 0.40, "w": 1.0, "fresh": 1.0, "label": "low relevance, max weight"},
        ]
        scored = [
            (m["label"], compute_full_score(m["sim"], m["w"], m["fresh"]))
            for m in memories
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        labels = [s[0] for s in scored]

        # The most relevant + high weight should be #1
        assert labels[0] == "very relevant, high weight"
        # Low relevance should never be #1 or #2 regardless of weight
        assert "low relevance, max weight" not in labels[:2]
