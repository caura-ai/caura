"""P6-1: Per-type decay rates — different memory types decay at different speeds.

Unit tests validate:
- TYPE_DECAY_DAYS covers all memory types
- Each type maps to a reasonable decay window
- Preferences and decisions decay slower than tasks and cancellations
- Freshness formula produces correct values for each type at key age points
- Fallback to FRESHNESS_DECAY_DAYS for unknown types
- Validity window overrides still take precedence
"""

import pytest

from core_api.constants import (
    FRESHNESS_DECAY_DAYS,
    FRESHNESS_FLOOR,
    MEMORY_TYPES,
    TYPE_DECAY_DAYS,
)


# ---------------------------------------------------------------------------
# Helper: compute freshness without DB (pure math)
# ---------------------------------------------------------------------------

def compute_freshness(
    age_days: float,
    memory_type: str,
    has_valid_end: bool = False,
    valid_end_expired: bool = False,
) -> float:
    """Replicate the SQL freshness formula in Python for testing."""
    if has_valid_end and valid_end_expired:
        return FRESHNESS_FLOOR
    if has_valid_end and not valid_end_expired:
        return 1.0
    # No validity window: type-aware decay
    decay_days = TYPE_DECAY_DAYS.get(memory_type, FRESHNESS_DECAY_DAYS)
    if age_days < decay_days:
        return 1.0 - (age_days / decay_days) * (1.0 - FRESHNESS_FLOOR)
    return FRESHNESS_FLOOR


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestTypeDecayConstants:
    """Verify TYPE_DECAY_DAYS configuration."""

    def test_all_memory_types_covered(self):
        """Every known memory type should have a decay value."""
        for mt in MEMORY_TYPES:
            assert mt in TYPE_DECAY_DAYS, f"Memory type '{mt}' missing from TYPE_DECAY_DAYS"

    def test_values_are_positive_integers(self):
        for mt, days in TYPE_DECAY_DAYS.items():
            assert isinstance(days, int), f"{mt}: decay days should be int, got {type(days)}"
            assert days > 0, f"{mt}: decay days should be positive"

    def test_preference_decays_slowest(self):
        assert TYPE_DECAY_DAYS["preference"] == max(TYPE_DECAY_DAYS.values())

    def test_cancellation_decays_fastest(self):
        assert TYPE_DECAY_DAYS["cancellation"] == min(TYPE_DECAY_DAYS.values())

    def test_decision_slower_than_episode(self):
        assert TYPE_DECAY_DAYS["decision"] > TYPE_DECAY_DAYS["episode"]

    def test_fact_slower_than_task(self):
        assert TYPE_DECAY_DAYS["fact"] > TYPE_DECAY_DAYS["task"]

    def test_fallback_is_90(self):
        """FRESHNESS_DECAY_DAYS (fallback) should be 90."""
        assert FRESHNESS_DECAY_DAYS == 90


@pytest.mark.unit
class TestTypeDecayFreshness:
    """Test freshness computation for different types at key age points."""

    def test_preference_at_30_days(self):
        """Preference (365-day window): at 30 days should be very fresh (>0.9)."""
        f = compute_freshness(30, "preference")
        assert f > 0.9, f"Preference at 30d should be >0.9, got {f:.4f}"

    def test_preference_at_180_days(self):
        """Preference at 180 days: still above 0.7."""
        f = compute_freshness(180, "preference")
        assert f > 0.7, f"Preference at 180d should be >0.7, got {f:.4f}"

    def test_task_at_30_days(self):
        """Task (30-day window): at 30 days should hit floor."""
        f = compute_freshness(30, "task")
        assert f == FRESHNESS_FLOOR, f"Task at 30d should be {FRESHNESS_FLOOR}, got {f:.4f}"

    def test_task_at_45_days(self):
        """Task past its window: should be at floor."""
        f = compute_freshness(45, "task")
        assert f == FRESHNESS_FLOOR

    def test_cancellation_at_14_days(self):
        """Cancellation (14-day window): at 14 days hits floor."""
        f = compute_freshness(14, "cancellation")
        assert f == FRESHNESS_FLOOR

    def test_cancellation_at_7_days(self):
        """Cancellation at 7 days: halfway through window."""
        f = compute_freshness(7, "cancellation")
        expected = 1.0 - (7.0 / 14.0) * (1.0 - FRESHNESS_FLOOR)
        assert abs(f - expected) < 0.001

    def test_decision_at_90_days(self):
        """Decision (180-day window): at 90 days should be above 0.7."""
        f = compute_freshness(90, "decision")
        assert f > 0.7

    def test_episode_at_45_days(self):
        """Episode (45-day window): at 45 days should hit floor."""
        f = compute_freshness(45, "episode")
        assert f == FRESHNESS_FLOOR

    def test_fact_at_60_days(self):
        """Fact (120-day window): at 60 days should be midway."""
        f = compute_freshness(60, "fact")
        expected = 1.0 - (60.0 / 120.0) * (1.0 - FRESHNESS_FLOOR)
        assert abs(f - expected) < 0.001

    def test_zero_age_always_fresh(self):
        """All types at age 0 should be 1.0."""
        for mt in MEMORY_TYPES:
            f = compute_freshness(0, mt)
            assert f == 1.0, f"{mt} at age 0 should be 1.0"

    def test_unknown_type_uses_fallback(self):
        """Unknown type should fall back to FRESHNESS_DECAY_DAYS (90)."""
        f = compute_freshness(45, "unknown_type")
        expected = 1.0 - (45.0 / 90.0) * (1.0 - FRESHNESS_FLOOR)
        assert abs(f - expected) < 0.001

    def test_decay_is_monotonic(self):
        """Freshness should monotonically decrease with age (per type)."""
        for mt in MEMORY_TYPES:
            prev = 2.0
            for age in range(0, 400, 5):
                f = compute_freshness(age, mt)
                assert f <= prev, f"{mt} at {age}d ({f}) should be <= {prev}"
                prev = f


@pytest.mark.unit
class TestValidityWindowOverride:
    """Validity window should always override type-based decay."""

    def test_expired_window_forces_floor(self):
        """Even a 365-day preference hits floor if validity window is expired."""
        f = compute_freshness(1, "preference", has_valid_end=True, valid_end_expired=True)
        assert f == FRESHNESS_FLOOR

    def test_active_window_forces_full(self):
        """Even a 14-day cancellation stays fresh if validity window is still active."""
        f = compute_freshness(100, "cancellation", has_valid_end=True, valid_end_expired=False)
        assert f == 1.0


@pytest.mark.unit
class TestTypeDecayOrdering:
    """Verify that freshness ordering matches intuitive expectations."""

    def test_preference_fresher_than_task_at_30_days(self):
        fp = compute_freshness(30, "preference")
        ft = compute_freshness(30, "task")
        assert fp > ft, "Preference should be fresher than task at 30 days"

    def test_decision_fresher_than_episode_at_60_days(self):
        fd = compute_freshness(60, "decision")
        fe = compute_freshness(60, "episode")
        assert fd > fe, "Decision should be fresher than episode at 60 days"

    def test_fact_fresher_than_action_at_30_days(self):
        ff = compute_freshness(30, "fact")
        fa = compute_freshness(30, "action")
        assert ff > fa, "Fact should be fresher than action at 30 days"
