"""L3 resolution mapping + safety-invariant tests (A55)."""

from __future__ import annotations

import pytest
from core_api.services.contradiction.resolution import ACTIONS, resolve

pytestmark = pytest.mark.unit


# ── base (relationship x diagnosis) -> action ─────────────────────────
@pytest.mark.parametrize(
    "relationship,diagnosis,expected",
    [
        ("exact_value", "temporal_change", "supersede"),
        ("exact_value", "correction", "replace"),
        ("negation", "correction", "replace"),
        ("scope_apparent", "scope_difference", "scope"),
        ("exact_value", "entity_mismatch", "split_entity"),
        ("exact_value", "write_error", "mark_disputed"),
        ("exact_value", "unresolved", "ask"),
    ],
)
def test_base_mapping(relationship, diagnosis, expected):
    r = resolve(relationship, diagnosis)
    assert r.action == expected
    assert r.action in ACTIONS
    assert r.audit_reason


# ── both-hold relationships never overturn ────────────────────────────
@pytest.mark.parametrize("rel", ["entailed", "constraint", "refinement"])
def test_both_hold_is_noop(rel):
    # even if the diagnosis would otherwise supersede/replace
    assert resolve(rel, "temporal_change").action == "no_op"
    assert resolve(rel, "correction").action == "no_op"


# ── invariant: inference must not overturn an explicit fact ───────────
def test_inferred_downgrades_supersede_to_mark_disputed():
    r = resolve("exact_value", "temporal_change", is_inferred=True)
    assert r.action == "mark_disputed"
    assert "inferred" in r.audit_reason


def test_inferred_downgrades_replace_to_mark_disputed():
    assert (
        resolve("exact_value", "correction", is_inferred=True).action == "mark_disputed"
    )


def test_inferred_does_not_touch_non_destructive_actions():
    # scope isn't destructive -> inference flag leaves it alone
    assert (
        resolve("scope_apparent", "scope_difference", is_inferred=True).action
        == "scope"
    )


# ── invariant: probabilistic relationship softens ─────────────────────
def test_probabilistic_downweights_instead_of_overturning():
    r = resolve("probabilistic", "temporal_change")
    assert r.action == "downweight"
    assert "probabilistic" in r.audit_reason


# ── invariant: low confidence flags rather than overturns ─────────────
def test_low_confidence_flags():
    assert (
        resolve("exact_value", "correction", confidence=0.3).action == "mark_disputed"
    )


def test_high_confidence_allows_overturn():
    assert resolve("exact_value", "correction", confidence=0.9).action == "replace"


def test_confidence_gate_only_applies_to_destructive():
    # scope is non-destructive; low confidence doesn't change it
    assert (
        resolve("scope_apparent", "scope_difference", confidence=0.1).action == "scope"
    )


# ── no-degradation anchor: today's confirmed conflicts map to supersede ─
def test_temporal_change_maps_to_supersede_matching_today():
    """The RDF/semantic paths today produce a supersede effect. temporal_change
    (the RDF default diagnosis) -> supersede keeps the recommendation aligned."""
    assert resolve("exact_value", "temporal_change").action == "supersede"
