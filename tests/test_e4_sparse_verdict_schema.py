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

pytestmark = pytest.mark.unit

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


def test_entity_aware_prompt_asks_for_sparse_reply() -> None:
    prompt = BATCH_ENTITY_AWARE_CONTRADICTION_PROMPT
    assert '"clean"' in prompt
    assert '"hits"' in prompt
    assert "relationship" not in prompt
    assert "diagnosis" not in prompt


# ---------------------------------------------------------------------------
# The content judge's "checks" contract: every verdict stated once
# ---------------------------------------------------------------------------

PASS = {
    "things": "the gateway / the gateway",
    "same_thing": True,
    "slots": "region / region",
    "one_value_only": True,
    "same_slot": True,
    "different_value": True,
    "non_conflict_reason": "none",
    "change": "eu-west -> us-east",
}


def test_content_prompt_asks_for_checks() -> None:
    prompt = BATCH_CONTRADICTION_PROMPT
    assert '"checks"' in prompt
    for key in ("same_thing", "same_slot", "different_value", "non_conflict_reason"):
        assert f'"{key}"' in prompt
    assert '"hits"' not in prompt
    assert "relationship" not in prompt
    assert "diagnosis" not in prompt


def test_checks_expand_to_one_judgment_per_candidate() -> None:
    raws = _expand_sparse_batch(
        {"checks": {"0": "-", "1": PASS, "2": {**PASS, "same_slot": False}}}, 3
    )
    assert raws[0] == {"contradicts": False}
    assert raws[1] == {
        "same_subject": True,
        "non_conflict_reason": "none",
        "contradicts": True,
    }
    assert raws[2] == {"contradicts": False}
    assert _judge_contradiction(raws[1]) == (True, 0.90)


def test_checks_need_every_answer_to_be_literally_true() -> None:
    for key in ("same_thing", "same_slot", "different_value"):
        for bad in (False, "true", 1, None):
            raws = _expand_sparse_batch({"checks": {"0": {**PASS, key: bad}}}, 1)
            assert raws == [{"contradicts": False}], (key, bad)


def test_checks_missing_or_malformed_entries_are_clean() -> None:
    raws = _expand_sparse_batch({"checks": {"1": "junk", 2: PASS}}, 4)
    assert raws[0] == raws[1] == raws[3] == {"contradicts": False}
    assert raws[2]["contradicts"] is True  # int keys tolerated


def test_checks_slot_that_holds_several_values_is_not_a_contradiction() -> None:
    # The prompt: same_slot is false "when one_value_only is false". A reply
    # that says the slot holds several values yet calls it the same slot
    # contradicts itself, and is not a contradiction of the memories.
    for bad in (False, "true", None, 0):
        raws = _expand_sparse_batch(
            {"checks": {"0": {**PASS, "one_value_only": bad}}}, 1
        )
        assert raws == [{"contradicts": False}], bad


def test_checks_without_a_change_is_not_a_contradiction() -> None:
    # The prompt: change is "" "when nothing is replaced", so a different
    # value with nothing replaced contradicts itself. Staging, 2026-11-24:
    # "The Catalog Service cache expiry was cut from 60 to 10 minutes" was
    # flagged against "The Catalog Service caches product lists for 10
    # minutes", which agree.
    for change in ("", "   ", "10 minutes -> 10 minutes", " 10 Minutes ->10 minutes "):
        raws = _expand_sparse_batch({"checks": {"0": {**PASS, "change": change}}}, 1)
        assert raws == [{"contradicts": False}], change


def test_checks_real_updates_still_contradict() -> None:
    # The four updates the staging demo planted, as the judge states them, and
    # the arrow the prompt asks for written as the character.
    for change in (
        "100 requests per minute -> 600 requests per minute",
        "02:00 UTC -> 04:30 UTC",
        "2 December -> 9 December",
        "30 minutes -> 15 minutes",
        "30 minutes → 15 minutes",
    ):
        raws = _expand_sparse_batch({"checks": {"0": {**PASS, "change": change}}}, 1)
        assert raws[0]["contradicts"] is True, change
        assert _judge_contradiction(raws[0]) == (True, 0.90)


def test_checks_change_without_two_values_is_not_a_contradiction() -> None:
    # The prompt asks for "<candidate's value> -> <NEW statement's value>".
    # Replayed on staging's judge, every reply that passed a real update did
    # so; one that passed a look-alike wrote "silent timeout / emits ...".
    for change in (
        "raised to 600",
        "silent timeout / emits export_duration_seconds",
        "a -> b -> c",
    ):
        raws = _expand_sparse_batch({"checks": {"0": {**PASS, "change": change}}}, 1)
        assert raws == [{"contradicts": False}], change


def test_checks_reply_without_the_new_keys_is_judged_as_before() -> None:
    # A provider that leaves out one_value_only or change isn't vetoed for it:
    # the gate fires only on a reply that states the opposite.
    entry = {k: v for k, v in PASS.items() if k not in ("one_value_only", "change")}
    raws = _expand_sparse_batch({"checks": {"0": entry}}, 1)
    assert raws[0]["contradicts"] is True


async def _judge_one(new: str, candidate: str, change: str) -> bool:
    """Run one candidate through ``_llm_contradiction_check_batch`` with a
    judge that answers yes to every check and states ``change``."""
    fake_llm = AsyncMock()
    fake_llm.complete_json = AsyncMock(
        return_value={"checks": {"0": {**PASS, "change": change}}}
    )

    async def _one_call(**kw):
        return await kw["call_fn"](fake_llm)

    with patch(
        "core_api.services.contradiction_detector.call_with_fallback",
        side_effect=_one_call,
    ):
        raws = await _llm_contradiction_check_batch(new, [{"content": candidate}])
    return _judge_contradiction(raws[0])[0]


# Staging, 2026-11-24 (Vertex gemini-3.1-flash-lite): pairs the judge passed
# whose change isn't one between the two statements.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("new", "candidate", "change"),
    [
        (  # the 60 is the NEW statement's history; both now say 10 minutes
            "The Catalog Service cache expiry was cut from 60 to 10 minutes after the stale price incident.",
            "The Catalog Service caches product lists in Redis for 10 minutes.",
            "60 minutes -> 10 minutes",
        ),
        (  # neither statement says absent or present
            "The retry storm on 14 November came from three services sharing one Payments Client config"
            " without jitter.",
            "Jitter was added to the Payments Client backoff after the retry storm on 14 November.",
            "absent -> present",
        ),
        (  # a value that only adds a word refines the other
            "Since the SessionCache rename, a code search for session store finds only the Postgres table.",
            "The session store interface was renamed SessionCache, because it only caches sessions;"
            " Postgres stays the source of truth.",
            "Postgres -> Postgres table",
        ),
        (  # the same event in another tense
            "Acme is acquiring Globex.",
            "Acme acquired Globex last March.",
            "acquired last March -> is acquiring",
        ),
    ],
)
async def test_checks_change_must_be_between_the_two_statements(
    new: str, candidate: str, change: str
) -> None:
    assert await _judge_one(new, candidate, change) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("new", "candidate", "change"),
    [
        # The four updates the staging demo planted, with the judge's change.
        (
            "The Invoices API rate limit is now 600 requests per minute per tenant; it was raised for the"
            " Billing Dashboard's polling.",
            "The Invoices API rate limit is 100 requests per minute per tenant.",
            "100 requests per minute per tenant -> 600 requests per minute per tenant",
        ),
        (
            "The Nightly Export now runs at 04:30 UTC, after the analytics replica finishes its vacuum.",
            "The Nightly Export runs at 02:00 UTC.",
            "02:00 UTC -> 04:30 UTC",
        ),
        (
            "Mobile Release 4.2 moved to 9 December, so the Face ID crash fix can finish beta.",
            "Mobile Release 4.2 ships to the App Store and Google Play on 2 December.",
            "2 December -> 9 December",
        ),
        (
            "SessionCache entries now expire after 15 minutes of inactivity, after the security review.",
            "SessionCache entries expire after 30 minutes of inactivity.",
            "30 minutes -> 15 minutes",
        ),
        # A negation is a change even though it only adds a word.
        (
            "Force-pushing to main is not allowed.",
            "Force-pushing to main is allowed.",
            "allowed -> not allowed",
        ),
        # A value reworded in another tense still comes from its statement.
        (
            "The gateway was moved to us-east.",
            "The gateway runs in eu-west.",
            "runs in eu-west -> moved to us-east",
        ),
        # Another number is a change even though the words are the same.
        ("The Atlas SDK is now on v1.1.", "The Atlas SDK is on v1.", "v1 -> v1.1"),
    ],
)
async def test_checks_change_between_the_two_statements_still_contradicts(
    new: str, candidate: str, change: str
) -> None:
    assert await _judge_one(new, candidate, change) is True


def test_content_prompt_asks_for_values_in_their_own_words() -> None:
    # The change check compares each value with its statement, so the prompt
    # asks for them as worded there, and says history and causes add to a
    # value rather than replace it.
    assert (
        "each in the\n     words its own statement uses" in BATCH_CONTRADICTION_PROMPT
    )
    assert (
        '"was cut\n   from 60 to 10 minutes" agrees with "is 10 minutes"'
        in BATCH_CONTRADICTION_PROMPT
    )


def test_checks_reason_still_vetoes_through_the_shared_gate() -> None:
    raws = _expand_sparse_batch(
        {"checks": {"0": {**PASS, "non_conflict_reason": "refinement"}}}, 1
    )
    assert raws[0]["non_conflict_reason"] == "refinement"
    verdict, _ = _judge_contradiction(raws[0])
    assert verdict is False


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
