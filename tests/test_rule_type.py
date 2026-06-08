"""P2-4: Rule memory type — prescriptive directives, policies, constraints.

Unit tests validate:
- 'rule' in MEMORY_TYPES and regex pattern
- TYPE_DECAY_DAYS assigns 365d (near-permanent, same as preference)
- Keyword heuristic classifies prescriptive content as rule
- Keyword heuristic does NOT misclassify descriptive content as rule
- Freshness values at key age points

Benchmark:
- Enrichment heuristic latency unchanged with new keywords
"""

import re
import time

import pytest

from core_api.constants import (
    FRESHNESS_DECAY_DAYS,
    FRESHNESS_FLOOR,
    MEMORY_TYPES,
    MEMORY_TYPES_PATTERN,
    TYPE_DECAY_DAYS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_freshness(age_days: float, memory_type: str) -> float:
    """Replicate the SQL freshness formula in Python."""
    decay_days = TYPE_DECAY_DAYS.get(memory_type, FRESHNESS_DECAY_DAYS)
    if age_days < decay_days:
        return 1.0 - (age_days / decay_days) * (1.0 - FRESHNESS_FLOOR)
    return FRESHNESS_FLOOR


# ---------------------------------------------------------------------------
# Unit tests: Constants
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRuleConstants:
    """Verify rule type is properly registered in constants."""

    def test_rule_in_memory_types(self):
        assert "rule" in MEMORY_TYPES

    def test_rule_matches_pattern(self):
        assert re.match(MEMORY_TYPES_PATTERN, "rule")

    def test_pattern_still_rejects_invalid(self):
        assert not re.match(MEMORY_TYPES_PATTERN, "trigger")
        assert not re.match(MEMORY_TYPES_PATTERN, "constraint")

    def test_rule_in_type_decay_days(self):
        assert "rule" in TYPE_DECAY_DAYS

    def test_rule_decay_365_days(self):
        """Rules should decay over 365 days — near-permanent directives."""
        assert TYPE_DECAY_DAYS["rule"] == 365

    def test_rule_same_decay_as_preference(self):
        """Rules and preferences are both long-lived knowledge."""
        assert TYPE_DECAY_DAYS["rule"] == TYPE_DECAY_DAYS["preference"]

    def test_memory_types_count(self):
        """Should now have 14 types (added 'insight')."""
        assert len(MEMORY_TYPES) == 14


# ---------------------------------------------------------------------------
# Unit tests: Keyword heuristic
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRuleKeywordHeuristic:
    """Verify the fake enrichment correctly classifies prescriptive content."""

    def test_always_notify(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich(
            "Always notify the security team before deploying to production"
        )
        assert result.memory_type == "rule"

    def test_never_store(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("Never store PII in Redis — use encrypted Postgres only")
        assert result.memory_type == "rule"

    def test_must_always(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("Agents must always log their actions to the audit trail")
        assert result.memory_type == "rule"

    def test_whenever_trigger(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich(
            "Whenever a customer mentions churn, escalate to retention team"
        )
        assert result.memory_type == "rule"

    def test_policy_prefix(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("Policy: all API keys must be rotated every 90 days")
        assert result.memory_type == "rule"

    def test_guideline_prefix(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("Guideline: use semantic versioning for all releases")
        assert result.memory_type == "rule"

    def test_do_not_ever(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("Do not ever commit secrets to the repository")
        assert result.memory_type == "rule"

    def test_never_do(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("Never do a production deploy on Fridays")
        assert result.memory_type == "rule"

    def test_never_commit(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("Never commit secrets to the repository")
        assert result.memory_type == "rule"

    def test_rule_weight_is_high(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("Never use plaintext passwords in config files")
        assert result.weight == 0.85

    def test_rule_status_is_active(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("Always do a code review before merging")
        assert result.status == "active"

    # --- Negative cases: should NOT be classified as rule ---

    def test_plain_fact_not_rule(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("The server is running on port 8080")
        assert result.memory_type != "rule"

    def test_bare_never_not_rule(self):
        """'never' in narrative context should NOT trigger rule classification."""
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("The deploy never succeeded due to network issues")
        assert result.memory_type != "rule", (
            "Bare 'never' in narrative should not be classified as rule"
        )

    def test_bare_whenever_not_rule(self):
        """'whenever' in narrative context should NOT trigger rule classification."""
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("Whenever we had outages, it was the DB")
        assert result.memory_type != "rule", (
            "Bare 'whenever' in narrative should not be classified as rule"
        )

    def test_decision_not_rule(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("We decided to use PostgreSQL for the main database")
        assert result.memory_type == "decision"

    def test_task_not_rule(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("I need to review the pull request by Friday")
        assert result.memory_type == "task"

    def test_episode_not_rule(self):
        from core_api.services.memory_enrichment import _fake_enrich

        result = _fake_enrich("We deployed version 2.3 to production yesterday")
        assert result.memory_type == "episode"


# ---------------------------------------------------------------------------
# Unit tests: Freshness
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRuleFreshness:
    """Verify freshness behavior for rule type."""

    def test_rule_at_0_days(self):
        assert compute_freshness(0, "rule") == 1.0

    def test_rule_at_90_days(self):
        """Rule at 90 days should be much fresher than a fact at 90 days."""
        rule_f = compute_freshness(90, "rule")
        fact_f = compute_freshness(90, "fact")
        assert rule_f > fact_f, "Rule should be fresher than fact at 90 days"

    def test_rule_at_180_days(self):
        """Rule should still be above 0.8 at 180 days."""
        f = compute_freshness(180, "rule")
        assert f > 0.8, f"Rule at 180d should be >0.8, got {f:.4f}"

    def test_rule_at_365_days(self):
        """Rule hits floor at exactly 365 days."""
        f = compute_freshness(365, "rule")
        assert f == FRESHNESS_FLOOR

    def test_rule_vs_task_at_30_days(self):
        """Rule should be much fresher than a task at 30 days."""
        rule_f = compute_freshness(30, "rule")
        task_f = compute_freshness(30, "task")
        assert rule_f > task_f


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------


@pytest.mark.benchmark
class TestRuleHeuristicBenchmark:
    """Ensure adding rule keywords doesn't slow down the heuristic."""

    ITERATIONS = 10_000

    def test_heuristic_latency_with_rule(self):
        from core_api.services.memory_enrichment import _fake_enrich

        samples = [
            "Always notify security before production deploys",
            "Never store PII in Redis",
            "The server runs on port 8080",
            "We decided to use PostgreSQL",
            "Task: review the pull request",
        ]

        t0 = time.perf_counter_ns()
        for i in range(self.ITERATIONS):
            _fake_enrich(samples[i % len(samples)])
        elapsed_us = (time.perf_counter_ns() - t0) / 1000

        per_call_us = elapsed_us / self.ITERATIONS
        print(
            f"\n  Enrichment heuristic: {per_call_us:.3f}μs/call ({self.ITERATIONS} iterations)"
        )
        assert per_call_us < 50.0, f"Heuristic too slow: {per_call_us:.2f}μs/call"
