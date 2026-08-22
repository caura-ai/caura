"""With no LLM, contradiction detection must ABSTAIN rather than guess.

A ``True`` verdict here is not advisory: ``detect_contradictions`` acts on the
boolean alone and marks the older memory ``conflicted``, which demotes it out of
semantic recall. The precise cost — including the exact-lexical-match carve-out
that keeps some conflicted rows, and how narrow Path C's restoration actually is —
is documented once, on ``_skip_contradiction_pairwise``. Not restated here, so the
two cannot drift.

The old fallback made that call from a negation-word heuristic whose own docstring
said "for testing". These tests pin that it no longer can.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core_api.services.contradiction_detector import (
    _CONF_FALLBACK,
    _fake_contradiction_check,
    _judge_contradiction,
    _llm_contradiction_check,
    _llm_contradiction_check_batch,
    _llm_entity_aware_contradiction_check,
    _llm_entity_aware_contradiction_check_batch,
)

pytestmark = [pytest.mark.unit]


class _NoLLM:
    """Feature explicitly disabled — ``none`` means off, so the judge abstains."""

    entity_extraction_provider = "none"


def _outage_config():
    """The production shape: a configured REAL provider whose key is missing, so it
    resolves to FakeLLMProvider and lands on the fallback. MagicMock because
    ``call_with_fallback`` and the provider factory read several attributes."""
    config = MagicMock()
    config.entity_extraction_provider = "openai"
    config.openai_api_key = None
    config.anthropic_api_key = None
    config.gemini_api_key = None
    config.openrouter_api_key = None
    return config


# Chosen so the OLD fallback would have returned True: negation present on
# exactly one side, and 3+ shared non-negation words. The control test below
# proves that, so the abstain assertions cannot pass vacuously on bland input.
_NEW = "the helios migration is not finished this quarter"
_OLD = "the helios migration is finished this quarter"


def test_the_heuristic_really_would_have_flagged_this() -> None:
    """Control. Without this, the tests below prove nothing about the inputs."""
    assert _fake_contradiction_check(_NEW, _OLD) is True, (
        "test inputs must be heuristic-positive, or the abstain assertions are vacuous"
    )


@pytest.mark.asyncio
async def test_pairwise_fallback_abstains() -> None:
    verdict, confidence = await _llm_contradiction_check(_NEW, _OLD, tenant_config=_NoLLM())

    assert verdict is False, (
        "with no LLM the fallback must abstain — a True marks the older memory "
        "conflicted, which removes it from recall with no path back"
    )
    # The rubric's documented "could not tell" confidence, same as the value
    # _judge_contradiction returns for a malformed response.
    assert confidence == _CONF_FALLBACK


@pytest.mark.asyncio
async def test_batch_fallback_abstains_for_every_candidate() -> None:
    candidates = [
        {"id": "a", "content": _OLD},
        {"id": "b", "content": "something unrelated entirely"},
    ]

    out = await _llm_contradiction_check_batch(_NEW, candidates, tenant_config=_NoLLM())

    assert len(out) == len(candidates), "one entry per candidate, or _align mis-pairs them"
    # Both call sites feed each raw to _judge_contradiction, so pin what they
    # actually derive. This also catches the 0.90 shape: {"contradicts": False} is a
    # non-empty dict and scores _CONF_CLEAN, which reads as a confident clean verdict.
    assert [_judge_contradiction(e) for e in out] == [(False, _CONF_FALLBACK)] * len(candidates)


@pytest.mark.asyncio
async def test_batch_fallback_never_claims_same_subject() -> None:
    """The old fallback hardcoded ``same_subject: True`` for every candidate.

    That contradicted the batch prompt's own rule — "Set true ONLY when subject_a
    and subject_b refer to the SAME real-world entity", and "If same_subject is
    false, contradicts MUST be false" — for candidates it had never compared.
    """
    candidates = [{"id": "a", "content": _OLD}, {"id": "b", "content": "unrelated"}]

    out = await _llm_contradiction_check_batch(_NEW, candidates, tenant_config=_NoLLM())

    assert not any(e.get("same_subject") is True for e in out), (
        f"abstaining must not assert same-subject for uncompared candidates; got {out!r}"
    )


@pytest.mark.asyncio
async def test_pairwise_abstains_on_a_real_provider_outage() -> None:
    """The case that actually matters: a real provider configured, key missing."""
    verdict, confidence = await _llm_contradiction_check(_NEW, _OLD, _outage_config())

    assert verdict is False
    assert confidence == _CONF_FALLBACK


@pytest.mark.asyncio
async def test_batch_abstains_on_a_real_provider_outage() -> None:
    out = await _llm_contradiction_check_batch(
        _NEW, [{"id": "a", "content": _OLD}], _outage_config()
    )

    assert [_judge_contradiction(e) for e in out] == [(False, _CONF_FALLBACK)]
    assert not any(e.get("same_subject") is True for e in out)


# The entity-aware judges are the two sites Path C uses, and every existing
# entity-aware test patches call_with_fallback or the batch function outright, so
# fake_fn is never reached there. Without these, either site could be reverted to
# the heuristic with the whole suite still green.


@pytest.mark.asyncio
async def test_entity_aware_pairwise_abstains() -> None:
    verdict, confidence = await _llm_entity_aware_contradiction_check(
        _NEW, _OLD, [{"name": "helios migration"}], [{"name": "helios migration"}],
        _outage_config(),
    )

    assert verdict is False
    assert confidence == _CONF_FALLBACK


@pytest.mark.asyncio
async def test_entity_aware_batch_abstains() -> None:
    out = await _llm_entity_aware_contradiction_check_batch(
        _NEW,
        [{"name": "helios migration"}],
        [{"id": "a", "content": _OLD}],
        _outage_config(),
    )

    assert [_judge_contradiction(e) for e in out] == [(False, _CONF_FALLBACK)]
    assert not any(e.get("same_subject") is True for e in out)
