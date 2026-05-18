"""CAURA-111: Subject-equality gate in the contradiction-detection prompt.

The old prompt asked the LLM "do these contradict?" without requiring the
model to first check whether the two memories share a subject. That allowed
cross-subject false positives whenever two memories of *different* subjects
sat close in embedding space (e.g., "Sarah Johnson prefers X" vs
"David Patel prefers not-X", or "Daniel Cohen joined Acme" vs
"Daniel Levi left Acme").

The fix has two parts, both covered here:

  1. The prompt forces structured output with ``subject_a``, ``subject_b``,
     ``same_subject``, ``contradicts``, ``reason``.
  2. ``_parse_contradiction_response`` enforces a hard gate: if the model
     ever returns ``contradicts=true`` while ``same_subject=false``, the
     parser overrides to False. This is defense-in-depth — the prompt
     should prevent the inconsistent combo, but if a future prompt
     regression or model quirk produces it, the parser still blocks the
     cross-subject false positive from reaching the caller.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core_api.services.contradiction_detector import (
    CONTRADICTION_PROMPT,
    _llm_contradiction_check,
    _parse_contradiction_response,
)


# ---------------------------------------------------------------------------
# Parser hard-gate — pure function, no LLM
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseContradictionResponse:
    """The parser must enforce ``same_subject AND contradicts`` semantics."""

    def test_same_subject_and_contradicts_returns_true(self):
        assert _parse_contradiction_response({
            "subject_a": "Alice",
            "subject_b": "Alice",
            "same_subject": True,
            "contradicts": True,
            "reason": "Alice's stated city changed from Tel Aviv to Haifa.",
        }) is True

    def test_same_subject_no_contradiction_returns_false(self):
        assert _parse_contradiction_response({
            "subject_a": "Alice",
            "subject_b": "Alice",
            "same_subject": True,
            "contradicts": False,
            "reason": "Same subject, complementary facts.",
        }) is False

    def test_different_subjects_no_contradiction_returns_false(self):
        assert _parse_contradiction_response({
            "subject_a": "Sarah Johnson",
            "subject_b": "David Patel",
            "same_subject": False,
            "contradicts": False,
            "reason": "Different people; preferences about different subjects.",
        }) is False

    def test_hard_gate_blocks_inconsistent_combination(self):
        """The Sarah/David failure mode: model emits contradicts=true even
        though same_subject=false. The parser MUST override to False."""
        assert _parse_contradiction_response({
            "subject_a": "Sarah Johnson",
            "subject_b": "David Patel",
            "same_subject": False,
            "contradicts": True,  # model misbehavior
            "reason": "opposite preferences (wrong — different subjects)",
        }) is False

    def test_missing_same_subject_treated_as_false(self):
        """Conservative default: missing key -> not a contradiction."""
        assert _parse_contradiction_response({
            "contradicts": True,
            "reason": "no same_subject field",
        }) is False

    def test_missing_contradicts_treated_as_false(self):
        assert _parse_contradiction_response({
            "same_subject": True,
        }) is False

    def test_empty_dict_returns_false(self):
        assert _parse_contradiction_response({}) is False

    def test_non_dict_input_returns_false(self):
        """Defensive: malformed JSON, None, etc., must not crash or pass."""
        assert _parse_contradiction_response(None) is False  # type: ignore[arg-type]
        assert _parse_contradiction_response("contradicts") is False  # type: ignore[arg-type]
        assert _parse_contradiction_response(["contradicts", True]) is False  # type: ignore[arg-type]

    def test_string_false_does_not_bypass_gate(self):
        """A model returning the JSON STRING "false" (instead of the boolean
        false) must not be coerced to truthy. ``bool("false")`` is True in
        Python; the parser must use an identity check against True to avoid
        silently letting cross-subject false positives through."""
        # Both fields as the string "false" — must read as False.
        assert _parse_contradiction_response({
            "subject_a": "X",
            "subject_b": "Y",
            "same_subject": "false",
            "contradicts": "false",
            "reason": "model emitted strings instead of booleans",
        }) is False
        # The dangerous shape: model says same_subject=false (string) but
        # also contradicts=true (string). Without identity check this would
        # have been read as same_subject=True AND contradicts=True -> True.
        assert _parse_contradiction_response({
            "subject_a": "Sarah",
            "subject_b": "David",
            "same_subject": "false",
            "contradicts": "true",
            "reason": "would have leaked through bool() coercion",
        }) is False
        # Non-True truthy values must also not pass.
        assert _parse_contradiction_response({
            "same_subject": 1,
            "contradicts": 1,
        }) is False


# ---------------------------------------------------------------------------
# Prompt content — structural invariants
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPromptContent:
    """Lock in the structural properties of the new prompt so a future edit
    cannot silently regress the subject-equality contract."""

    def test_prompt_has_required_placeholders(self):
        assert "{new_content}" in CONTRADICTION_PROMPT
        assert "{old_content}" in CONTRADICTION_PROMPT

    def test_prompt_requires_all_structured_fields(self):
        for key in ("subject_a", "subject_b", "same_subject", "contradicts", "reason"):
            assert key in CONTRADICTION_PROMPT, f"prompt missing required field: {key}"

    def test_prompt_states_different_subjects_rule(self):
        """The core rule that prevents cross-subject false positives."""
        text = CONTRADICTION_PROMPT.lower()
        assert "different subjects" in text
        assert "not a contradiction" in text or "not_a_contradiction" in text

    def test_prompt_warns_about_shared_first_or_last_name(self):
        """The Daniel Cohen / Daniel Levi failure mode is named explicitly."""
        text = CONTRADICTION_PROMPT.lower()
        assert "first name" in text or "last name" in text

    def test_prompt_forbids_markdown_fences(self):
        """JSON-only output protects the downstream parser."""
        assert "no markdown fences" in CONTRADICTION_PROMPT.lower()

    def test_prompt_keeps_legacy_update_rule(self):
        """Updates / corrections about the same subject ARE contradictions —
        regression guard so we don't over-correct in the opposite direction."""
        text = CONTRADICTION_PROMPT.lower()
        assert "updates" in text or "corrections" in text

    def test_prompt_temporal_rule_is_exception_based(self):
        """The temporal rule must put the burden on the historical exception
        (both statements must explicitly reference non-overlapping past
        periods) and must forbid the model from speculating that a date
        stamp marks a future state which resolves the conflict. Locks in
        the post-wet-test refinement so the rule can't silently revert to
        the looser NLI-style 'different time periods are not contradictions'
        framing that misfired in the Gemini wet test."""
        text = CONTRADICTION_PROMPT.lower()
        assert (
            "non-overlapping" in text or "historically true" in text
        ), "temporal rule must carve out genuinely historical periods only"
        assert (
            "speculate" in text or "future" in text
        ), "temporal rule must forbid future-state speculation that dissolves conflicts"


# ---------------------------------------------------------------------------
# End-to-end gate via _llm_contradiction_check with a stub LLM
# ---------------------------------------------------------------------------


class _StubLLM:
    """Minimal LLM provider that returns a canned JSON response."""

    provider_name = "stub"
    model = "stub"
    is_fake = False

    def __init__(self, payload: dict):
        self._payload = payload

    async def complete_json(self, prompt: str, *, temperature: float = 0.0) -> dict:
        return self._payload


async def _run_check_with_payload(payload: dict) -> bool:
    """Invoke ``_llm_contradiction_check`` with the real ``_do_check`` body
    but bypass the provider-resolution / fallback chain by stubbing
    ``call_with_fallback`` to invoke the supplied ``call_fn`` against a
    ``_StubLLM(payload)``. This exercises the full prompt-format → parse path.
    """

    async def fake_call_with_fallback(*, call_fn, **_kwargs):
        return await call_fn(_StubLLM(payload))

    with patch(
        "core_api.services.contradiction_detector.call_with_fallback",
        side_effect=fake_call_with_fallback,
    ):
        return await _llm_contradiction_check(
            new_content="ignored — stub returns payload directly",
            old_content="ignored — stub returns payload directly",
        )


@pytest.mark.unit
class TestLlmContradictionCheckEndToEnd:
    """End-to-end: parser hard-gate must hold through the real call path."""

    async def test_sarah_vs_david_blocked_even_if_model_says_contradicts(self):
        """Documented failure pair #1. Even if the model emits the
        inconsistent ``contradicts=true`` for a clearly cross-subject pair,
        the parser blocks it."""
        result = await _run_check_with_payload({
            "subject_a": "Sarah Johnson",
            "subject_b": "David Patel",
            "same_subject": False,
            "contradicts": True,
            "reason": "model misbehavior — opposite predicates, different people",
        })
        assert result is False

    async def test_daniel_cohen_vs_daniel_levi_blocked(self):
        """Documented failure pair #2. Shared first name, different people."""
        result = await _run_check_with_payload({
            "subject_a": "Daniel Cohen",
            "subject_b": "Daniel Levi",
            "same_subject": False,
            "contradicts": True,
            "reason": "model misbehavior — shared first name, different individuals",
        })
        assert result is False

    async def test_genuine_same_subject_contradiction_still_fires(self):
        """Regression guard the other way: a real same-subject update must
        still be flagged as a contradiction."""
        result = await _run_check_with_payload({
            "subject_a": "Alice",
            "subject_b": "Alice",
            "same_subject": True,
            "contradicts": True,
            "reason": "Alice moved from Tel Aviv to Haifa.",
        })
        assert result is True

    async def test_same_subject_complementary_not_contradiction(self):
        result = await _run_check_with_payload({
            "subject_a": "Alice",
            "subject_b": "Alice",
            "same_subject": True,
            "contradicts": False,
            "reason": "Both facts about Alice; complementary.",
        })
        assert result is False
