"""A4 #12 — ``_llm_contradiction_check`` returns ``(verdict, confidence)``.

Pattern-Y enabler: the same LLM-judges-pair-returns-verdict-with-
confidence shape gets reused by A4 #13 (Path C retraction's
confidence-weighted veto) and by A1 #16 (dedup danger-zone judge).
This PR widens the return type and pins a confidence score derived
from the model's own coherence — high when the response is internally
consistent, lower when the parser's safety gates had to override the
raw verdict.

Confidence rubric (all in [0.0, 1.0]):

  0.90 — Clean LLM agreement: ``same_subject`` matches the verdict
         direction, ``non_conflict_reason`` is ``"none"`` (or the
         model said ``contradicts=False`` against a recognised
         non-conflict shape — both gates aligned).
  0.85 — Gate 2 fired: model said ``contradicts=True`` AND named a
         recognised ``non_conflict_reason`` from
         ``NON_CONFLICT_REASONS``. Parser overrides to False. The
         model's own pattern recognition flagged the non-conflict
         shape — high trust in NOT-a-contradiction.
  0.60 — Gate 1 fired: model said ``contradicts=True`` with
         ``same_subject=False``. Parser overrides to False. The
         model contradicted itself between the two fields — lower
         trust.
  0.50 — Heuristic fallback or unparseable response. Low trust,
         downstream callers should hesitate.

Tests pin both the return shape (tuple) and the per-branch confidence
values. They FAIL against current main (function still returns bool).
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Return-shape tests
# ---------------------------------------------------------------------------


def test_judge_returns_tuple_bool_float() -> None:
    """``_judge_contradiction`` is the new (verdict, confidence) helper."""
    from core_api.services.contradiction_detector import _judge_contradiction

    out = _judge_contradiction(
        {
            "same_subject": True,
            "contradicts": True,
            "non_conflict_reason": "none",
            "subject_a": "x",
            "subject_b": "x",
            "reason": "test",
        }
    )
    assert isinstance(out, tuple)
    assert len(out) == 2
    verdict, confidence = out
    assert isinstance(verdict, bool)
    assert isinstance(confidence, float)
    assert 0.0 <= confidence <= 1.0


# ---------------------------------------------------------------------------
# Confidence rubric — per-branch
# ---------------------------------------------------------------------------


def test_clean_agreement_high_confidence() -> None:
    """Same_subject=True + contradicts=True + non_conflict_reason='none'
    → model committed fully → 0.90."""
    from core_api.services.contradiction_detector import _judge_contradiction

    verdict, conf = _judge_contradiction(
        {
            "same_subject": True,
            "contradicts": True,
            "non_conflict_reason": "none",
        }
    )
    assert verdict is True
    assert conf == pytest.approx(0.90)


def test_clean_not_contradiction_high_confidence() -> None:
    """Same_subject=True + contradicts=False + non_conflict_reason='none'.
    Model said no conflict directly; nothing in the response is
    inconsistent. Same confidence as clean-True case."""
    from core_api.services.contradiction_detector import _judge_contradiction

    verdict, conf = _judge_contradiction(
        {
            "same_subject": True,
            "contradicts": False,
            "non_conflict_reason": "none",
        }
    )
    assert verdict is False
    assert conf == pytest.approx(0.90)


def test_gate2_fired_returns_high_confidence_not_contradiction() -> None:
    """Model said contradicts=True AND named a non-conflict pattern.
    Parser overrides to False; the model's own pattern recognition
    flagged the not-contradiction shape — high trust in the final
    NOT-contradiction verdict."""
    from core_api.services.contradiction_detector import _judge_contradiction

    verdict, conf = _judge_contradiction(
        {
            "same_subject": True,
            "contradicts": True,
            "non_conflict_reason": "temporal_supersession",
        }
    )
    assert verdict is False
    assert conf == pytest.approx(0.85)


def test_gate1_fired_returns_medium_confidence_not_contradiction() -> None:
    """Model said contradicts=True with same_subject=False. Parser
    overrides to False — but the model's response was internally
    inconsistent (cross-subject hallucinated contradiction), so
    confidence is lower."""
    from core_api.services.contradiction_detector import _judge_contradiction

    verdict, conf = _judge_contradiction(
        {
            "same_subject": False,
            "contradicts": True,
            "non_conflict_reason": "none",
        }
    )
    assert verdict is False
    assert conf == pytest.approx(0.60)


def test_clean_not_contradiction_cross_subject_high_confidence() -> None:
    """Same_subject=False + contradicts=False — model agreed with itself
    on cross-subject ⇒ no contradiction. Clean case."""
    from core_api.services.contradiction_detector import _judge_contradiction

    verdict, conf = _judge_contradiction(
        {
            "same_subject": False,
            "contradicts": False,
            "non_conflict_reason": "none",
        }
    )
    assert verdict is False
    assert conf == pytest.approx(0.90)


def test_malformed_response_low_confidence() -> None:
    """Non-dict / missing keys → conservative False with low confidence.
    Downstream callers can use this floor to gate "don't act on
    untrustworthy verdicts"."""
    from core_api.services.contradiction_detector import _judge_contradiction

    verdict, conf = _judge_contradiction("not a dict")  # type: ignore[arg-type]
    assert verdict is False
    assert conf == pytest.approx(0.50)

    verdict, conf = _judge_contradiction({})  # empty dict — also unparseable
    assert verdict is False
    assert conf == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# Caller-facing contract — _llm_contradiction_check returns the tuple
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_contradiction_check_returns_tuple() -> None:
    """The async entry point ``_llm_contradiction_check`` (called from
    ``detect_contradictions_async`` and
    ``detect_contradictions_by_entities_async``) returns the same
    ``(verdict, confidence)`` tuple."""
    from unittest.mock import AsyncMock, patch

    from core_api.services.contradiction_detector import _llm_contradiction_check

    fake_llm = AsyncMock()
    fake_llm.complete_json = AsyncMock(
        return_value={
            "same_subject": True,
            "contradicts": True,
            "non_conflict_reason": "none",
            "subject_a": "alice",
            "subject_b": "alice",
            "reason": "lives in different cities",
        }
    )

    async def fake_call_with_fallback(**kw):
        return await kw["call_fn"](fake_llm)

    with patch(
        "core_api.services.contradiction_detector.call_with_fallback",
        new=AsyncMock(side_effect=fake_call_with_fallback),
    ):
        result = await _llm_contradiction_check("A says X", "B says Y")

    assert isinstance(result, tuple)
    assert len(result) == 2
    verdict, confidence = result
    assert verdict is True
    assert confidence == pytest.approx(0.90)
