"""A1 #15 — two-tier dedup threshold constants.

Today's dedup is single-tier: ``SEMANTIC_DEDUP_THRESHOLD = 0.95``.
At cosine similarity ≥ 0.95 the write is rejected with 409
"near-duplicate exists"; everything else is accepted. A single
threshold can't distinguish:

  - "almost certainly identical text" (e.g. trivial whitespace differs)
    — should auto-reject without LLM cost
  - "looks similar, could be a near-dup or could be a refinement"
    — should call an LLM judge (A1 #16) to decide
  - "clearly different memories that happen to share vocabulary"
    — should accept without action

A1 #15 introduces two NEW constants that define a three-tier
decision band; A1 #16 will wire the dispatch.

  SEMANTIC_DEDUP_AUTO_THRESHOLD = 0.97
    Above this: auto-reject (almost certainly the same content). Tight
    enough that we won't false-reject genuine refinements.

  SEMANTIC_DEDUP_JUDGE_THRESHOLD = 0.85
    Above this (but below auto): danger zone — A1 #16 dispatches the
    LLM judge with A4 #12's ``(verdict, confidence)`` shape.
    Below this: not a duplicate, accept.

This PR adds the constants and the invariants that protect their
ordering. It does NOT change any existing call site —
``SEMANTIC_DEDUP_THRESHOLD = 0.95`` stays put and the single-tier
code path keeps working. A1 #16 is the one that flips dispatch onto
the new band.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Existence
# ---------------------------------------------------------------------------


def test_auto_threshold_exists() -> None:
    """Above ``SEMANTIC_DEDUP_AUTO_THRESHOLD`` → auto-reject without LLM."""
    from common.constants import SEMANTIC_DEDUP_AUTO_THRESHOLD

    assert isinstance(SEMANTIC_DEDUP_AUTO_THRESHOLD, float)


def test_judge_threshold_exists() -> None:
    """At/above ``SEMANTIC_DEDUP_JUDGE_THRESHOLD`` (but below auto) →
    A1 #16 will dispatch the LLM judge. Below it → accept."""
    from common.constants import SEMANTIC_DEDUP_JUDGE_THRESHOLD

    assert isinstance(SEMANTIC_DEDUP_JUDGE_THRESHOLD, float)


# ---------------------------------------------------------------------------
# Values — picked thresholds; codify so future tuning surfaces in code review.
# ---------------------------------------------------------------------------


def test_auto_threshold_value() -> None:
    """0.97 — bumped from today's single-tier 0.95 so the auto-reject
    band is reserved for "almost certainly identical text". Genuine
    refinements (same first sentence + extra detail in the second)
    typically sit 0.85-0.95 — those move to the judge band in A1 #16,
    not auto-reject."""
    from common.constants import SEMANTIC_DEDUP_AUTO_THRESHOLD

    assert SEMANTIC_DEDUP_AUTO_THRESHOLD == 0.97


def test_judge_threshold_value() -> None:
    """0.85 — broad enough to catch refinements (the case auto-reject
    used to miss-classify) but tight enough that the LLM judge isn't
    swamped with unrelated memories that share vocabulary."""
    from common.constants import SEMANTIC_DEDUP_JUDGE_THRESHOLD

    assert SEMANTIC_DEDUP_JUDGE_THRESHOLD == 0.85


# ---------------------------------------------------------------------------
# Ordering invariants — surface tuning mistakes at import time.
# ---------------------------------------------------------------------------


def test_thresholds_in_valid_cosine_range() -> None:
    """Cosine similarity is bounded [-1, 1]; practical bounds are [0, 1]."""
    from common.constants import (
        SEMANTIC_DEDUP_AUTO_THRESHOLD,
        SEMANTIC_DEDUP_JUDGE_THRESHOLD,
    )

    for t in (SEMANTIC_DEDUP_AUTO_THRESHOLD, SEMANTIC_DEDUP_JUDGE_THRESHOLD):
        assert 0.0 < t < 1.0, f"threshold out of range: {t}"


def test_auto_is_strictly_above_judge() -> None:
    """Auto-reject band must sit above the judge band — otherwise the
    danger-zone dispatch in A1 #16 would be unreachable."""
    from common.constants import (
        SEMANTIC_DEDUP_AUTO_THRESHOLD,
        SEMANTIC_DEDUP_JUDGE_THRESHOLD,
    )

    assert SEMANTIC_DEDUP_AUTO_THRESHOLD > SEMANTIC_DEDUP_JUDGE_THRESHOLD


def test_legacy_threshold_unchanged() -> None:
    """``SEMANTIC_DEDUP_THRESHOLD = 0.95`` is the single-tier value
    consumed by the existing write-path dedup. A1 #15 does NOT change
    it — A1 #16 is the PR that flips dispatch onto the new tiered
    constants. Keeping the legacy constant in place means this PR is
    risk-free for in-flight writes."""
    from common.constants import SEMANTIC_DEDUP_THRESHOLD

    assert SEMANTIC_DEDUP_THRESHOLD == 0.95


def test_judge_threshold_below_legacy_threshold() -> None:
    """The judge band starts BELOW today's single-tier cutoff. That's
    the whole point — A1 #16 will pick up cases the old logic accepted
    (because they were under 0.95) and route them to the LLM if they
    sit between 0.85 and 0.95. Without this ordering the new tier
    couldn't see any cases the old tier missed."""
    from common.constants import (
        SEMANTIC_DEDUP_JUDGE_THRESHOLD,
        SEMANTIC_DEDUP_THRESHOLD,
    )

    assert SEMANTIC_DEDUP_JUDGE_THRESHOLD < SEMANTIC_DEDUP_THRESHOLD
