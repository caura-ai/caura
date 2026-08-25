"""Unit tests for ``common.entity_naming`` — the WT-2 canonical match key.

The rule under test (see the module docstring for the full rationale):

- normalise: lowercase, strip, collapse internal whitespace;
- iteratively strip ONE leading determiner/temporal qualifier
  (``the a an new old current existing legacy``) per step, ONLY while the
  remainder keeps >= 2 tokens.

The two-token guard is the safety property: ``new york`` must never reduce
to ``york`` — in either comparison direction.
"""

from __future__ import annotations

import pytest

from common.entity_naming import canonical_match_key, normalize_entity_name

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# normalize_entity_name — case / whitespace only
# ---------------------------------------------------------------------------


def test_normalize_lowercases_and_collapses_whitespace():
    assert normalize_entity_name("  New   Analytics\tService ") == "new analytics service"


def test_normalize_touches_nothing_else():
    # Punctuation and internal structure are preserved — conservative on
    # purpose (no punctuation stripping in this fix; see PR body).
    assert normalize_entity_name("PostgreSQL 16") == "postgresql 16"
    assert normalize_entity_name("gpt-5.4-nano") == "gpt-5.4-nano"


# ---------------------------------------------------------------------------
# canonical_match_key — the WT-2 merge rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # The wet-test pair itself.
        ("new analytics service", "analytics service"),
        # Case / whitespace variants.
        ("Analytics Service", "analytics   service"),
        # Determiner.
        ("the analytics service", "analytics service"),
        # Stacked qualifiers strip one per step.
        ("the new analytics service", "analytics service"),
        # Other qualifiers from the fixed set.
        ("legacy billing system", "billing system"),
        ("current payment gateway", "payment gateway"),
    ],
)
def test_same_subject_maps_to_same_key(a: str, b: str):
    assert canonical_match_key(a) == canonical_match_key(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # THE guard case: stripping "new" would leave the 1-token "york".
        ("new york", "york"),
        # Same guard for pure determiners.
        ("the office", "office"),
        ("an apple", "apple"),
        # Qualifier stacking stops at the guard: "the new york" -> "new york",
        # which still differs from "york".
        ("the new york", "york"),
        # Non-qualifier leading words never strip.
        ("data analytics service", "analytics service"),
        # Substrings never match — no substring heuristics.
        ("analytics", "analytics service"),
    ],
)
def test_distinct_subjects_keep_distinct_keys(a: str, b: str):
    assert canonical_match_key(a) != canonical_match_key(b)


def test_the_new_york_reduces_to_new_york_not_york():
    # Explicit shape check (not just inequality): the per-step guard stops
    # exactly when the remainder would drop below two tokens.
    assert canonical_match_key("the new york") == "new york"
    assert canonical_match_key("new york") == "new york"
    assert canonical_match_key("york") == "york"


def test_symmetry_of_the_rule():
    # The rule is key-to-key, so it cannot be asymmetric — pin that anyway.
    a, b = "new analytics service", "analytics service"
    assert (canonical_match_key(a) == canonical_match_key(b)) == (
        canonical_match_key(b) == canonical_match_key(a)
    )
