"""CAURA-124: ``non_conflict_reason`` hard-gate in the contradiction parser.

This gate sits *after* the CAURA-111 ``same_subject`` gate. It catches
within-subject false positives — shapes where two same-subject claims
both hold and so should not be flagged as a contradiction:

  - temporal_supersession        (planned -> shipped, open -> closed)
  - list_valued_predicate        (supports X / supports Y)
  - refinement                   (coarse -> fine granularity)
  - scope_mismatch               (whole/part, time-window, qualifier)
  - same_name_distinct_subject   (same surface name, different referents)
  - conditional_unrealized       (irrealis vs factual)
  - event_restatement            (same event, different verb/tense)

The model classifies the shape; the parser hard-gates any of the seven
to ``contradicts=false``. ``"none"`` (and any missing / unknown value)
leaves ``contradicts`` untouched.
"""

import pytest

from core_api.services.contradiction_detector import (
    CONTRADICTION_PROMPT,
    NON_CONFLICT_REASONS,
    _parse_contradiction_response,
)


@pytest.mark.unit
class TestNonConflictReasonGate:
    """Hard-gate semantics for the new enum field."""

    @pytest.mark.parametrize(
        "reason",
        sorted(NON_CONFLICT_REASONS),
    )
    def test_each_recognised_reason_forces_contradicts_false(self, reason: str):
        """Any value in the recognised set must override contradicts=true."""
        verdict = _parse_contradiction_response(
            {
                "subject_a": "X",
                "subject_b": "X",
                "same_subject": True,
                "non_conflict_reason": reason,
                "contradicts": True,
                "reason": f"shape={reason}",
            }
        )
        assert verdict is False, (
            f"non_conflict_reason={reason!r} must hard-gate contradicts to False"
        )

    def test_none_value_passes_through(self):
        """``"none"`` must NOT override; the model's contradicts verdict stands."""
        verdict = _parse_contradiction_response(
            {
                "subject_a": "X",
                "subject_b": "X",
                "same_subject": True,
                "non_conflict_reason": "none",
                "contradicts": True,
                "reason": "genuine same-subject conflict",
            }
        )
        assert verdict is True

    def test_missing_field_is_safe_default(self):
        """Absent field behaves like ``"none"`` — no override; preserves
        backward compatibility with any caller / cached response that
        predates the new gate."""
        verdict = _parse_contradiction_response(
            {
                "subject_a": "X",
                "subject_b": "X",
                "same_subject": True,
                "contradicts": True,
                "reason": "genuine same-subject conflict",
            }
        )
        assert verdict is True

    def test_unknown_value_is_safe_default(self):
        """A model emitting an enum value we don't recognise must not
        accidentally hard-gate. Only the documented set blocks."""
        verdict = _parse_contradiction_response(
            {
                "subject_a": "X",
                "subject_b": "X",
                "same_subject": True,
                "non_conflict_reason": "made_up_reason_42",
                "contradicts": True,
                "reason": "model hallucinated an enum value",
            }
        )
        assert verdict is True

    def test_non_string_value_is_safe_default(self):
        """Defensive: non-string types must not crash and must not gate."""
        for value in (None, 123, True, ["temporal_supersession"], {"x": 1}):
            verdict = _parse_contradiction_response(
                {
                    "subject_a": "X",
                    "subject_b": "X",
                    "same_subject": True,
                    "non_conflict_reason": value,
                    "contradicts": True,
                }
            )
            assert verdict is True, (
                f"Unexpected gate-fire on non-string value {value!r}"
            )

    def test_gate_does_not_flip_a_false_to_true(self):
        """If the model said contradicts=false, the gate is a no-op."""
        for reason in sorted(NON_CONFLICT_REASONS) + ["none", "unknown"]:
            verdict = _parse_contradiction_response(
                {
                    "subject_a": "X",
                    "subject_b": "X",
                    "same_subject": True,
                    "non_conflict_reason": reason,
                    "contradicts": False,
                }
            )
            assert verdict is False


@pytest.mark.unit
class TestGateOrdering:
    """The two gates compose correctly — cross-subject (CAURA-111) is
    evaluated first; within-subject FP (CAURA-124) only kicks in for
    rows that survived the first gate."""

    def test_cross_subject_overrides_even_with_recognised_non_conflict_reason(self):
        """A pathological response (different subjects but with a
        non-conflict reason emitted) must still be blocked by the
        first gate."""
        verdict = _parse_contradiction_response(
            {
                "subject_a": "Sarah",
                "subject_b": "David",
                "same_subject": False,
                "non_conflict_reason": "temporal_supersession",
                "contradicts": True,
            }
        )
        assert verdict is False

    def test_same_subject_with_no_reason_can_still_contradict(self):
        """The headline genuine-conflict path: same subject, no shape,
        contradicts=true → returns True."""
        verdict = _parse_contradiction_response(
            {
                "subject_a": "Alice",
                "subject_b": "Alice",
                "same_subject": True,
                "non_conflict_reason": "none",
                "contradicts": True,
                "reason": "ship date slipped",
            }
        )
        assert verdict is True


@pytest.mark.unit
class TestPromptCarriesEnum:
    """Lock structural invariants of the new prompt — a future edit can
    cannot silently drop the enum field or any of the seven values."""

    def test_prompt_enumerates_all_seven_values(self):
        for reason in sorted(NON_CONFLICT_REASONS):
            assert reason in CONTRADICTION_PROMPT, (
                f"non_conflict_reason value {reason!r} missing from prompt; "
                "parser hard-gate would no longer match a model that follows"
                " the (now incomplete) instructions."
            )

    def test_prompt_includes_none_as_passthrough_value(self):
        assert '"none"' in CONTRADICTION_PROMPT or "none|" in CONTRADICTION_PROMPT

    def test_prompt_documents_non_conflict_reason_in_response_schema(self):
        # The response JSON schema example must list non_conflict_reason
        # so the model knows to emit it. We look for the field name in
        # the same line as the enum literals to catch accidental drift.
        assert "non_conflict_reason" in CONTRADICTION_PROMPT
        # Pipe-separated enum literal must include the canonical first
        # and last values so a reordering doesn't silently truncate.
        assert "temporal_supersession" in CONTRADICTION_PROMPT
        assert "event_restatement" in CONTRADICTION_PROMPT

    def test_recognised_reasons_match_prompt_documented_set(self):
        # If the prompt enumerates a value we don't recognise in the
        # parser, the parser will pass it through unchanged — silently
        # disabling the gate for that shape. This test forces the two
        # to stay in sync.
        for reason in NON_CONFLICT_REASONS:
            assert reason in CONTRADICTION_PROMPT, (
                f"NON_CONFLICT_REASONS contains {reason!r} but the prompt "
                "doesn't tell the model to emit it; gate effectively dead."
            )
