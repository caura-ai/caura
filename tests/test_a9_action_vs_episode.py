"""A9 — type classifier confuses ``action`` and ``episode``.

Gap: 28 of ~95 misclassifications are ``action``-class; ``action``
accounts for 30% of all type errors. Root cause: the enrichment
prompt distinguishes neither dimension cleanly between

  - **action** — the actor's own completed deed (past tense first-
    person: "I deployed", "filed", "merged", "sent"). Subject is
    typically the agent itself.

  - **episode** — an observed event tied to time/place that the
    actor witnessed or recorded (third-person: "the deployment
    failed", "the meeting concluded", "outage between 14:00 and
    14:30"). Subject is the event, not the agent.

Fix: 2-3 contrastive few-shot pairs in the enrichment prompt + a
sharpened type description so the LLM has explicit guidance on the
dimension that disambiguates them. No schema change.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def _get_prompt() -> str:
    from core_api.services.memory_enrichment import ENRICHMENT_PROMPT

    return ENRICHMENT_PROMPT


# ---------------------------------------------------------------------------
# Prompt pins: action/episode disambiguation guidance is present.
# ---------------------------------------------------------------------------


def test_prompt_mentions_action_vs_episode_distinction():
    """The prompt must explicitly call out the contrast — not just
    list both types with their generic descriptions."""
    prompt = _get_prompt().lower()
    # The disambiguation guidance lives in some "action vs episode"
    # or "episode vs action" block. Accept either ordering.
    has_disambiguation_block = (
        "action vs episode" in prompt
        or "episode vs action" in prompt
        or "action versus episode" in prompt
    )
    assert has_disambiguation_block, (
        "prompt must include an explicit action-vs-episode disambiguation block"
    )


def test_prompt_mentions_actor_or_first_person_for_action():
    """Action's distinguishing feature is "the actor's own deed" —
    prompt should mention first-person / actor / "I" / "my" framing
    so the LLM can pick up the signal."""
    prompt = _get_prompt().lower()
    signals = [
        "first-person",
        "first person",
        "actor's own",
        "by the actor",
        "i did",
        '"i "',
    ]
    assert any(s in prompt for s in signals), (
        f"prompt must signal first-person/actor framing for action; tried {signals}"
    )


def test_prompt_mentions_observed_or_witnessed_for_episode():
    """Episode's distinguishing feature is an observed/witnessed
    event tied to time — prompt should hint at it."""
    prompt = _get_prompt().lower()
    signals = [
        "observed",
        "witnessed",
        "happened",
        "occurred",
        "third-person",
        "third person",
    ]
    assert any(s in prompt for s in signals), (
        f"prompt must signal observed/witnessed framing for episode; tried {signals}"
    )


def test_prompt_has_at_least_two_contrastive_examples():
    """The gap specifies '2-3 contrastive few-shot pairs'. Check
    that at least two example pairs (or equivalent) are present in
    the action/episode block."""
    prompt = _get_prompt()
    # Heuristic: at least two arrows / colons annotating example→type
    # in the disambiguation area. Look for phrases like ``→ action``,
    # ``→ episode``, ``-> action``, etc.
    import re

    arrow_action = re.findall(r"(?:→|->)\s*action\b", prompt)
    arrow_episode = re.findall(r"(?:→|->)\s*episode\b", prompt)
    assert len(arrow_action) >= 2, f"need ≥2 action examples; got {len(arrow_action)}"
    assert len(arrow_episode) >= 2, (
        f"need ≥2 episode examples; got {len(arrow_episode)}"
    )


def test_prompt_examples_are_realistic_business_content():
    """The examples should be the shapes that misclassify in real
    traffic — deployments, meetings, code merges, filed reports —
    not abstract placeholders like ``X did Y``."""
    prompt = _get_prompt().lower()
    realistic_terms = ["deploy", "meeting", "merge", "ship", "filed", "review"]
    matched = [t for t in realistic_terms if t in prompt]
    assert len(matched) >= 2, (
        f"examples should use realistic terms; only matched {matched}"
    )


def test_prompt_word_count_still_bounded():
    """A9 adds few-shot pairs (raises ceiling from 1100 → 1200 to
    cover A8's tag guidance + A9's action/episode pairs). Keeps cost
    bounded while permitting the new high-signal content."""
    prompt = _get_prompt()
    word_count = len(prompt.split())
    assert word_count < 1200, f"prompt is {word_count} words — too long"


# ---------------------------------------------------------------------------
# MEMORY_TYPE_DESCRIPTIONS — action/episode descriptions sharpened so
# the per-type bullet list rendered into the prompt carries the
# disambiguation signal too.
# ---------------------------------------------------------------------------


def test_action_description_mentions_actor():
    from common.enrichment.constants import MEMORY_TYPE_DESCRIPTIONS

    desc = MEMORY_TYPE_DESCRIPTIONS["action"].lower()
    # Must hint at "the actor did X" framing — not just "concrete steps".
    assert "actor" in desc or "first-person" in desc or "first person" in desc, (
        f"action description should signal actor framing; got: {desc!r}"
    )


def test_episode_description_mentions_observed_or_event():
    from common.enrichment.constants import MEMORY_TYPE_DESCRIPTIONS

    desc = MEMORY_TYPE_DESCRIPTIONS["episode"].lower()
    # Original already says "events that happened" — keep that;
    # confirm it stays event/observed-framed.
    assert "event" in desc or "observed" in desc, (
        f"episode description should signal observed/event framing; got: {desc!r}"
    )
