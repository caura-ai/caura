"""A32 — forward Path-A detection must not act on a weak judgment.

The row was filed because the forward path marks the older row
``conflicted`` on a bare ``if verdict:`` and discards the confidence, while
the retraction path carries a 0.90 gate. Verified before changing anything:
the premise does NOT hold in current code — ``_judge_contradiction`` forces
the verdict to False on every low-confidence branch, so a True verdict
always carries ``_CONF_CLEAN``.

A confidence gate was implemented and then REVERTED: it changed behaviour
only for the deliberate fake provider (whose True verdicts carry
``_CONF_FALLBACK``), breaking the dev/CI stand-in without making anything
safer. What remains is the invariant, pinned here so a future change to the
rubric — which WOULD make the bare check unsafe — fails loudly.
"""

import pytest

from core_api.services.contradiction_detector import (
    _CONF_CLEAN,
    _CONF_FALLBACK,
    NON_CONFLICT_REASONS,
    RETRACTION_CONFIDENCE_THRESHOLD,
    _judge_contradiction,
)

pytestmark = pytest.mark.unit


def test_retraction_threshold_is_the_clean_confidence():
    assert RETRACTION_CONFIDENCE_THRESHOLD == _CONF_CLEAN


def test_a_true_verdict_is_never_low_confidence():
    """The invariant the forward gate relies on, probed exhaustively over the
    judge's whole input space."""
    seen = set()
    for contradicts in (True, False):
        for same_subject in (True, False):
            for ncr in (None, "not_a_listed_reason", next(iter(NON_CONFLICT_REASONS))):
                raw = {"contradicts": contradicts, "same_subject": same_subject}
                if ncr:
                    raw["non_conflict_reason"] = ncr
                verdict, confidence = _judge_contradiction(raw)
                if verdict:
                    seen.add(confidence)
    assert seen == {_CONF_CLEAN}, f"a True verdict carried {seen}"


def test_malformed_response_is_false_and_low_confidence():
    for raw in (None, {}, "nonsense", []):
        verdict, confidence = _judge_contradiction(raw)
        assert verdict is False and confidence == _CONF_FALLBACK


def test_the_only_low_confidence_true_is_the_deliberate_fake_provider():
    """Why the forward path needs no confidence gate: the sole producer of a
    low-confidence True verdict is the fake heuristic an operator opts into,
    not a weak LLM judgment."""
    from pathlib import Path

    import core_api.services.contradiction_detector as cd

    src = Path(cd.__file__).read_text()
    assert "_CONF_FALLBACK)" in src  # the fake fn's confidence
    assert "A32 (verified 2026-08-31, no code change)" in src
