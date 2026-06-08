"""A1 #16 — CheckSemanticDuplicate dispatches the LLM judge in the
danger zone defined by A1 #15.

Decision band (from A1 #15):
  similarity ≥ AUTO (0.97)        → auto-reject 409 (no LLM call)
  JUDGE ≤ similarity < AUTO       → LLM judge decides (this PR)
  similarity < JUDGE (0.85)       → accept (no candidate returned at all)

The judge call mirrors A4 #12's pattern: a thin parser returns
``(is_duplicate, confidence)`` and an async wrapper threads through
``call_with_fallback`` for provider failover. Confidence below
``DEDUP_JUDGE_CONFIDENCE_THRESHOLD`` causes the write to be accepted
even when ``is_duplicate=True`` — a malformed/heuristic-fallback
response shouldn't 409 a legitimate write.

Tests cover:
- ``_judge_dedup`` parser confidence rubric (clean/malformed)
- ``_llm_dedup_check`` returns the tuple
- ``CheckSemanticDuplicate`` dispatches by tier, calling the judge
  only in the danger band and gating 409s on confidence
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Parser: _judge_dedup(raw) → (is_duplicate, confidence)
# ---------------------------------------------------------------------------


def test_judge_dedup_clean_true():
    """``is_duplicate=true`` with non-empty reason → clean response, high
    confidence."""
    from core_api.services.dedup_judge import _judge_dedup

    out = _judge_dedup({"is_duplicate": True, "reason": "trivial paraphrase"})
    assert isinstance(out, tuple)
    verdict, conf = out
    assert verdict is True
    assert conf == pytest.approx(0.90)


def test_judge_dedup_clean_false():
    """``is_duplicate=false`` with reason → clean, high confidence."""
    from core_api.services.dedup_judge import _judge_dedup

    verdict, conf = _judge_dedup({"is_duplicate": False, "reason": "refinement"})
    assert verdict is False
    assert conf == pytest.approx(0.90)


def test_judge_dedup_malformed_returns_low_confidence():
    """Non-dict or empty / missing fields → conservative False with
    fallback confidence floor."""
    from core_api.services.dedup_judge import _judge_dedup

    for raw in (None, "not a dict", {}, {"reason": "missing is_duplicate"}):
        verdict, conf = _judge_dedup(raw)  # type: ignore[arg-type]
        assert verdict is False
        assert conf == pytest.approx(0.50)


# ---------------------------------------------------------------------------
# Async wrapper: _llm_dedup_check returns the same tuple.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_dedup_check_returns_tuple():
    from core_api.services.dedup_judge import _llm_dedup_check

    fake_llm = AsyncMock()
    fake_llm.complete_json = AsyncMock(
        return_value={"is_duplicate": True, "reason": "exact paraphrase"}
    )

    async def fake_call_with_fallback(**kw):
        return await kw["call_fn"](fake_llm)

    with patch(
        "core_api.services.dedup_judge.call_with_fallback",
        new=AsyncMock(side_effect=fake_call_with_fallback),
    ):
        result = await _llm_dedup_check("new text", "old text")

    assert isinstance(result, tuple)
    verdict, conf = result
    assert verdict is True
    assert conf == pytest.approx(0.90)


# ---------------------------------------------------------------------------
# CheckSemanticDuplicate dispatch: tier logic
# ---------------------------------------------------------------------------


def _build_ctx(*, dedup_enabled: bool = True):
    """Minimal fake PipelineContext exercising the step's actual code path."""
    ctx = MagicMock()
    ctx.tenant_config = MagicMock(semantic_dedup_enabled=dedup_enabled)
    ctx.data = {
        "input": MagicMock(
            tenant_id="t1",
            fleet_id="f1",
            visibility="scope_team",
        ),
        "embedding": [0.1] * 10,
        "memory_fields": {"metadata": {}},
    }
    return ctx


@pytest.mark.asyncio
async def test_auto_reject_skips_llm_judge():
    """When ``_find_semantic_duplicate`` returns a candidate with
    similarity ≥ AUTO (0.97), the step MUST raise 409 immediately
    without calling the judge."""
    from fastapi import HTTPException

    from core_api.pipeline.steps.write.check_semantic_duplicate import (
        CheckSemanticDuplicate,
    )

    ctx = _build_ctx()
    candidate = {"id": "00000000-0000-0000-0000-000000000001", "similarity": 0.98}

    judge = AsyncMock()
    with (
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=AsyncMock(return_value=candidate),
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._llm_dedup_check",
            new=judge,
        ),
    ):
        step = CheckSemanticDuplicate()
        with pytest.raises(HTTPException) as ei:
            await step.execute(ctx)

    assert ei.value.status_code == 409
    judge.assert_not_called()


@pytest.mark.asyncio
async def test_judge_band_calls_llm_and_rejects_on_high_confidence_duplicate():
    """JUDGE ≤ similarity < AUTO → LLM judge is called; if it returns
    ``is_duplicate=True`` with confidence ≥ threshold, 409."""
    from fastapi import HTTPException

    from core_api.pipeline.steps.write.check_semantic_duplicate import (
        CheckSemanticDuplicate,
    )

    ctx = _build_ctx()
    candidate = {"id": "00000000-0000-0000-0000-000000000002", "similarity": 0.91}

    with (
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=AsyncMock(return_value=candidate),
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._llm_dedup_check",
            new=AsyncMock(return_value=(True, 0.90)),
        ),
    ):
        step = CheckSemanticDuplicate()
        with pytest.raises(HTTPException) as ei:
            await step.execute(ctx)

    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_judge_band_accepts_when_judge_says_not_duplicate():
    """Judge says ``is_duplicate=False`` → write is accepted."""
    from core_api.pipeline.steps.write.check_semantic_duplicate import (
        CheckSemanticDuplicate,
    )

    ctx = _build_ctx()
    candidate = {"id": "00000000-0000-0000-0000-000000000003", "similarity": 0.90}

    with (
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=AsyncMock(return_value=candidate),
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._llm_dedup_check",
            new=AsyncMock(return_value=(False, 0.90)),
        ),
    ):
        step = CheckSemanticDuplicate()
        # No exception → accept.
        result = await step.execute(ctx)
        assert result is None  # CheckSemanticDuplicate returns None on accept


@pytest.mark.asyncio
async def test_judge_band_accepts_on_low_confidence_even_if_verdict_duplicate():
    """``is_duplicate=True`` with confidence < threshold (e.g. heuristic
    fallback at 0.50) → write is accepted. We don't 409 a legitimate
    write on a malformed-LLM response."""
    from core_api.pipeline.steps.write.check_semantic_duplicate import (
        CheckSemanticDuplicate,
    )

    ctx = _build_ctx()
    candidate = {"id": "00000000-0000-0000-0000-000000000004", "similarity": 0.93}

    with (
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=AsyncMock(return_value=candidate),
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._llm_dedup_check",
            new=AsyncMock(return_value=(True, 0.50)),  # low confidence
        ),
    ):
        step = CheckSemanticDuplicate()
        assert await step.execute(ctx) is None


@pytest.mark.asyncio
async def test_no_candidate_returned_skips_llm_judge_and_accepts():
    """When ``_find_semantic_duplicate`` returns None (nothing above
    the JUDGE threshold), the step accepts without calling the judge."""
    from core_api.pipeline.steps.write.check_semantic_duplicate import (
        CheckSemanticDuplicate,
    )

    ctx = _build_ctx()
    judge = AsyncMock()
    with (
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._llm_dedup_check",
            new=judge,
        ),
    ):
        step = CheckSemanticDuplicate()
        assert await step.execute(ctx) is None
        judge.assert_not_called()


@pytest.mark.asyncio
async def test_dedup_disabled_skips_all():
    """When ``tenant_config.semantic_dedup_enabled`` is False, neither
    the find nor the judge are called. Existing back-compat contract."""
    from core_api.pipeline.steps.write.check_semantic_duplicate import (
        CheckSemanticDuplicate,
    )

    ctx = _build_ctx(dedup_enabled=False)
    find = AsyncMock()
    judge = AsyncMock()
    with (
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=find,
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._llm_dedup_check",
            new=judge,
        ),
    ):
        step = CheckSemanticDuplicate()
        result = await step.execute(ctx)

    find.assert_not_called()
    judge.assert_not_called()
    # ``execute`` returns a SKIPPED outcome in this branch.
    assert result is not None
