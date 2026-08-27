"""E4 — sparse batch-judge verdict schema.

The dense reply cost ~35 output tokens x 20 candidates (~700/call, measured
on prod-shaped batches against gpt-5.4-nano) when typically 0–4 candidates
contradict — that filler was ~80% of the judge's OpenAI bill. The sparse
contract answers with a full judgment ONLY for contradicting candidates
(``hits``) and a bare index list for the rest (``clean``); the two enum
fields the dense shape carried "for A55 reuse" (``relationship`` /
``diagnosis``) were verified unconsumed and are gone.

Pinned here:
- ``_expand_sparse_batch`` expands the sparse shape to the exact per-candidate
  dicts the dense parser produced — non-hits are ``{"contradicts": False}``,
  byte-identical downstream at ``_judge_contradiction``.
- The pre-E4 dense shape still parses (a fallback provider answering in the
  old format degrades to old behaviour, never to "everything is clean").
- Gate 1 / Gate 2 still veto an incoherent hit.
- Both batch prompts carry the sparse contract and no longer ask for the
  unconsumed fields.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core_api.services.contradiction_detector import (
    BATCH_CONTRADICTION_PROMPT,
    BATCH_ENTITY_AWARE_CONTRADICTION_PROMPT,
    _expand_sparse_batch,
    _judge_contradiction,
    _llm_contradiction_check_batch,
)

HIT = {"same_subject": True, "non_conflict_reason": "none", "contradicts": True}


# ---------------------------------------------------------------------------
# Parser: sparse shape
# ---------------------------------------------------------------------------


def test_sparse_expands_hits_and_cleans() -> None:
    raws = _expand_sparse_batch({"clean": [1, 2], "hits": {"0": HIT}}, 3)
    assert raws == [HIT, {"contradicts": False}, {"contradicts": False}]


def test_sparse_no_hits_all_clean() -> None:
    raws = _expand_sparse_batch({"clean": [0, 1], "hits": {}}, 2)
    assert raws == [{"contradicts": False}, {"contradicts": False}]
    assert all(_judge_contradiction(r) == (False, 0.90) for r in raws)


def test_sparse_omitted_index_defaults_safe() -> None:
    # Index 2 listed nowhere — same safe default as clean, never a crash.
    raws = _expand_sparse_batch({"clean": [1], "hits": {"0": HIT}}, 3)
    assert raws[2] == {"contradicts": False}


def test_sparse_tolerates_int_hit_keys_and_malformed_entries() -> None:
    raws = _expand_sparse_batch({"clean": [], "hits": {0: HIT, "1": "junk"}}, 2)
    assert raws[0] == HIT
    assert raws[1] == {"contradicts": False}


def test_sparse_hits_key_alone_is_sparse() -> None:
    raws = _expand_sparse_batch({"hits": {"1": HIT}}, 2)
    assert raws == [{"contradicts": False}, HIT]


def test_non_dict_reply_defaults_every_candidate() -> None:
    assert _expand_sparse_batch(["junk"], 2) == [{"contradicts": False}] * 2


# ---------------------------------------------------------------------------
# Parser: dense back-compat (pre-E4 shape)
# ---------------------------------------------------------------------------


def test_dense_shape_still_parses() -> None:
    dense = {"0": HIT, "1": {"contradicts": False}, "99": HIT}
    raws = _expand_sparse_batch(dense, 3)
    assert raws[0] == HIT
    assert raws[1] == {"contradicts": False}
    assert raws[2] == {"contradicts": False}  # missing -> safe default


# ---------------------------------------------------------------------------
# Gates still run on hits
# ---------------------------------------------------------------------------


def test_gate1_vetoes_cross_subject_hit() -> None:
    raws = _expand_sparse_batch(
        {"clean": [], "hits": {"0": {"same_subject": False, "contradicts": True}}}, 1
    )
    verdict, _conf = _judge_contradiction(raws[0])
    assert verdict is False


def test_gate2_vetoes_non_conflict_reason_hit() -> None:
    raws = _expand_sparse_batch(
        {
            "clean": [],
            "hits": {
                "0": {
                    "same_subject": True,
                    "non_conflict_reason": "list_valued_predicate",
                    "contradicts": True,
                }
            },
        },
        1,
    )
    verdict, _conf = _judge_contradiction(raws[0])
    assert verdict is False


# ---------------------------------------------------------------------------
# Prompts carry the sparse contract; the unconsumed fields are gone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt", [BATCH_CONTRADICTION_PROMPT, BATCH_ENTITY_AWARE_CONTRADICTION_PROMPT]
)
def test_prompts_ask_for_sparse_reply(prompt: str) -> None:
    assert '"clean"' in prompt
    assert '"hits"' in prompt
    assert "relationship" not in prompt
    assert "diagnosis" not in prompt


# ---------------------------------------------------------------------------
# End-to-end through the batch function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_function_expands_sparse_reply() -> None:
    cands = [{"content": "a"}, {"content": "b"}, {"content": "c"}]
    fake_llm = AsyncMock()
    fake_llm.complete_json = AsyncMock(
        return_value={"clean": [0, 2], "hits": {"1": HIT}}
    )

    async def _one_call(**kw):
        return await kw["call_fn"](fake_llm)

    with patch(
        "core_api.services.contradiction_detector.call_with_fallback",
        side_effect=_one_call,
    ):
        raws = await _llm_contradiction_check_batch("new", cands)

    assert len(raws) == 3
    assert [_judge_contradiction(r)[0] for r in raws] == [False, True, False]
