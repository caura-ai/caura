"""WT-1: ``/recall``'s ``summary`` must be the answer, not the reasoning scaffold.

Wet-test defect WT-1: ``RECALL_PROMPT`` deliberately elicits step-by-step
reasoning before the answer (load-bearing for recall accuracy on
LoCoMo/LongMemEval — the reasoning stays), but the raw completion was surfaced
as ``summary`` unfiltered. Callers paid ~5x the tokens and had to string-parse
for the trailing ``**Answer:**`` line themselves.

The fix: the prompt now instructs the model to end with one final
``**Answer:** <answer>`` line, and ``_extract_final_answer`` strips everything
up to and including the LAST such marker server-side. No marker → fail open
(full completion unchanged), so the ``_fake_recall`` fallback, truncated
completions, and marker-ignoring models all still surface something.

Two layers:

1. Unit: ``_extract_final_answer`` across marker variants and edge cases.
2. Service: ``summarize_memories`` with the LLM call mocked to return a
   scaffolded completion — ``summary`` must be just the answer, the response
   SHAPE (C4 ``items`` alias included) must not change, and ``diagnostic``
   must carry the raw completion under ``recall_raw``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import core_api.services.recall_service as rs_mod
from core_api.services.recall_service import (
    RECALL_PROMPT,
    _extract_final_answer,
    summarize_memories,
)

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# Unit: _extract_final_answer
# ---------------------------------------------------------------------------

# The wet-test repro shape: scaffold, then a final bold marker line.
_SCAFFOLDED = (
    "Step 1: Extract the relevant facts.\n"
    "- The memories state the platform database is PostgreSQL 16.\n"
    "Step 2: Reason over the facts.\n"
    "The question asks which database we use; the memories answer it directly.\n"
    "\n"
    "**Answer:** We use **PostgreSQL 16**."
)


def test_marker_present_returns_only_answer() -> None:
    assert _extract_final_answer(_SCAFFOLDED) == "We use **PostgreSQL 16**."


def test_marker_absent_fails_open_unchanged() -> None:
    completion = "The memories do not record a completion date."
    assert _extract_final_answer(completion) == completion


def test_fake_fallback_text_passes_through_untouched() -> None:
    # ``_fake_recall`` output carries no marker; it must survive verbatim.
    completion = "(LLM unavailable; top 3 memories unsynthesized) alpha beta gamma"
    assert _extract_final_answer(completion) == completion


def test_multiple_markers_last_one_wins() -> None:
    completion = (
        "Step 1: a draft.\n"
        "**Answer:** the draft answer\n"
        "Wait — re-checking the dates.\n"
        "**Answer:** the corrected answer"
    )
    assert _extract_final_answer(completion) == "the corrected answer"


def test_marker_mid_text_returns_everything_after_it() -> None:
    completion = "Step 1: reasoning. **Answer:** Anna owns the rollback plan.\n"
    assert _extract_final_answer(completion) == "Anna owns the rollback plan."


def test_bold_colon_outside_variant() -> None:
    completion = "reasoning...\n**Answer**: 42"
    assert _extract_final_answer(completion) == "42"


def test_non_bold_line_start_variant() -> None:
    completion = "Step 1: facts.\nStep 2: reasoning.\nAnswer: shipped on the 3rd"
    assert _extract_final_answer(completion) == "shipped on the 3rd"


def test_whitespace_around_marker_tolerated() -> None:
    completion = "reasoning\n  **Answer:**   padded answer  \n"
    assert _extract_final_answer(completion) == "padded answer"


def test_plain_answer_mid_sentence_is_not_a_marker() -> None:
    # Only the bold forms match mid-line; a plain "Answer:" must be
    # line-anchored so reasoning prose can't false-positive.
    completion = "The partial Answer: fragments were discarded during step 2."
    assert _extract_final_answer(completion) == completion


def test_marker_with_nothing_after_fails_open() -> None:
    # A completion truncated right at the marker must not become an
    # empty summary.
    completion = "Step 1: facts.\nStep 2: reasoning.\n**Answer:**"
    assert _extract_final_answer(completion) == completion


def test_answer_spanning_multiple_lines_is_kept_whole() -> None:
    completion = "reasoning\n**Answer:** line one\nline two"
    assert _extract_final_answer(completion) == "line one\nline two"


def test_prompt_instructs_the_marker() -> None:
    # The extraction only works if the prompt asks for the marker; pin the
    # instruction so a prompt edit can't silently orphan the parser.
    assert "**Answer:**" in RECALL_PROMPT


# ---------------------------------------------------------------------------
# Service: summarize_memories surfaces the answer, keeps the shape
# ---------------------------------------------------------------------------


class _Mem(SimpleNamespace):
    """The attribute surface ``summarize_memories`` actually touches (same
    stand-in as ``test_no_llm_fallbacks_declare_themselves``): the prompt
    formatter's fields, ``ts_valid_start`` for the sort, ``model_dump`` for
    the response assembly."""

    def model_dump(self, mode: str | None = None) -> dict:
        return {"content": self.content, "memory_type": self.memory_type}


def _mem(content: str) -> _Mem:
    return _Mem(
        content=content, memory_type="fact", title=None, status="active", ts_valid_start=None
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        recall_enabled=True,
        recall_provider="openai",
        recall_model="test-model",
    )


_EXPECTED_TOP_LEVEL_KEYS = {
    "query",
    "summary",
    "memory_count",
    "memories",
    "items",
    "recall_ms",
}


@pytest.mark.asyncio
async def test_summary_is_the_answer_not_the_scaffold(monkeypatch) -> None:
    """WT-1 regression: with a scaffolded LLM completion, ``summary`` carries
    ONLY the text after the final ``**Answer:**`` marker.

    This test FAILS on the unfixed code (summary == the full completion)."""
    monkeypatch.setattr(
        rs_mod, "call_with_fallback", AsyncMock(return_value=_SCAFFOLDED)
    )

    result = await summarize_memories(
        [_mem("The platform database is PostgreSQL 16.")],
        "which database do we use?",
        _config(),
    )

    assert result["summary"] == "We use **PostgreSQL 16**.", (
        f"summary must be the answer alone, got {result['summary']!r}"
    )
    assert "Step 1" not in result["summary"]
    # Shape unchanged — C4 ``items`` alias and the rest of the surface intact.
    assert _EXPECTED_TOP_LEVEL_KEYS.issubset(result.keys())
    assert result["items"] == result["memories"]
    assert result["memory_count"] == 1


@pytest.mark.asyncio
async def test_markerless_completion_surfaces_verbatim(monkeypatch) -> None:
    """Fail open at the service level too: no marker → summary is the full
    completion, unchanged."""
    completion = "The memories do not contain enough to answer."
    monkeypatch.setattr(
        rs_mod, "call_with_fallback", AsyncMock(return_value=completion)
    )

    result = await summarize_memories([_mem("unrelated fact")], "when?", _config())

    assert result["summary"] == completion


@pytest.mark.asyncio
async def test_diagnostic_carries_raw_completion(monkeypatch) -> None:
    """``diagnostic.recall_raw`` preserves the unfiltered completion next to
    the existing ``recall_prompt``, so the scaffold stays inspectable."""
    monkeypatch.setattr(
        rs_mod, "call_with_fallback", AsyncMock(return_value=_SCAFFOLDED)
    )

    result = await summarize_memories(
        [_mem("The platform database is PostgreSQL 16.")],
        "which database do we use?",
        _config(),
        diagnostic=True,
    )

    assert result["summary"] == "We use **PostgreSQL 16**."
    diag = result["diagnostic"]
    assert diag["recall_raw"] == _SCAFFOLDED
    assert diag["recall_prompt"] is not None
