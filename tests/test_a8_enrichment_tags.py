"""A8 — enrichment tag generation underperforms.

Gap measured: ``tags.mean_jaccard = 0.27`` vs ``0.50`` target across
500 cases. Prompt produced inconsistent tag count, pluralization,
and separators (``code review`` / ``code-review`` / ``code_review``
/ ``code_reviews``). Downstream features that join on tags see
drift between equivalent labels.

Two-part fix:

1. **Prompt tightening** — cap at 5 tags, require singular canonical
   form, fix the multi-word separator (kebab-case ``code-review``).
   Pure prompt-spec change; no schema break.

2. **Defensive schema validator** — even with a tightened prompt the
   LLM drifts. ``EnrichmentResult.tags`` now passes through a
   normaliser that lowercases, strips whitespace, replaces internal
   whitespace / underscores with hyphens, dedupes, drops empties,
   and caps at 5. The set the downstream tag-join sees is now
   stable regardless of the LLM's exact spelling.

Conservative on singularization: English -s heuristic breaks
``news`` / ``sales`` / ``headquarters`` etc., so the validator
does NOT singularize. The prompt asks the LLM to use singular form;
the validator only normalizes spacing/case.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Prompt — pins the three tightening requirements from the gap.
# ---------------------------------------------------------------------------


def _get_prompt() -> str:
    from core_api.services.memory_enrichment import ENRICHMENT_PROMPT

    return ENRICHMENT_PROMPT


def test_prompt_caps_tag_count_at_five():
    """``2-5`` (or similar phrasing) replaces the old ``2-6``."""
    prompt = _get_prompt()
    # The cap should be visible in the tags field description.
    # Accept any of: "1-5", "2-5", "up to 5", "max 5", "no more than 5".
    cap_phrases = ["1-5", "2-5", "up to 5", "max 5", "no more than 5", "at most 5"]
    assert any(p in prompt for p in cap_phrases), (
        f"prompt must cap tags at 5; tried {cap_phrases}"
    )
    # And the old upper bound is gone.
    assert "2-6" not in prompt, "old `2-6` cap should be removed"


def test_prompt_specifies_singular_form():
    """Prompt mentions singular form / canonical to nudge the LLM."""
    prompt = _get_prompt().lower()
    assert "singular" in prompt, "prompt must mention singular form for tags"


def test_prompt_specifies_kebab_case_separator():
    """For multi-word tag concepts, prompt requires kebab-case
    (``code-review``) so downstream tag joins see stable keys."""
    prompt = _get_prompt().lower()
    # Either "kebab" or an explicit hyphen example.
    has_kebab = "kebab" in prompt or "hyphen" in prompt
    assert has_kebab, "prompt must specify hyphen / kebab-case for multi-word tags"


def test_prompt_includes_a_tag_example():
    """At least one concrete example showing the correct form
    (e.g. ``code-review``) so the LLM has a concrete pattern to mimic."""
    prompt = _get_prompt()
    # Look for a hyphen-joined token in the tag-related section.
    # Any token of the shape ``word-word`` will do.
    import re

    assert re.search(r'"[a-z]+-[a-z]+"', prompt) or re.search(
        r"`[a-z]+-[a-z]+`", prompt
    ), "prompt should include a hyphen-joined tag example"


def test_prompt_word_count_remains_bounded():
    """Adding tag guidance must not bust the prompt budget. Ceiling
    raised to 1200 in A9 to cover the action/episode disambiguation
    block + A8's tag guidance."""
    prompt = _get_prompt()
    word_count = len(prompt.split())
    assert word_count < 1200, f"prompt is {word_count} words — too long"


# ---------------------------------------------------------------------------
# Schema validator — normalises and caps tags regardless of LLM output.
# ---------------------------------------------------------------------------


def test_validator_lowercases_tags():
    from common.enrichment.schema import EnrichmentResult

    r = EnrichmentResult(tags=["MEETING", "Code-Review", "DECISION"])
    assert r.tags == ["meeting", "code-review", "decision"]


def test_validator_replaces_internal_whitespace_with_hyphen():
    """``code review`` → ``code-review`` (the gap's primary drift case)."""
    from common.enrichment.schema import EnrichmentResult

    r = EnrichmentResult(tags=["code review", "design  doc"])
    assert "code-review" in r.tags
    assert "design-doc" in r.tags


def test_validator_replaces_underscore_with_hyphen():
    """``code_review`` → ``code-review`` (snake_case → kebab-case)."""
    from common.enrichment.schema import EnrichmentResult

    r = EnrichmentResult(tags=["code_review"])
    assert r.tags == ["code-review"]


def test_validator_strips_leading_trailing_whitespace():
    from common.enrichment.schema import EnrichmentResult

    r = EnrichmentResult(tags=["  meeting  ", "decision\n"])
    assert r.tags == ["meeting", "decision"]


def test_validator_drops_empty_after_strip():
    """``""`` / ``"   "`` after normalisation → dropped, not kept as empty string."""
    from common.enrichment.schema import EnrichmentResult

    r = EnrichmentResult(tags=["meeting", "", "   ", "decision"])
    assert r.tags == ["meeting", "decision"]


def test_validator_dedupes():
    """Normalisation collapses near-duplicates that the LLM might emit
    ("Code Review" + "code-review" + "code_review" → single entry)."""
    from common.enrichment.schema import EnrichmentResult

    r = EnrichmentResult(tags=["Code Review", "code-review", "code_review"])
    assert r.tags == ["code-review"]


def test_validator_caps_at_five():
    """LLM emits 7 tags; only the first 5 survive."""
    from common.enrichment.schema import EnrichmentResult

    r = EnrichmentResult(tags=["t1", "t2", "t3", "t4", "t5", "t6", "t7"])
    assert r.tags == ["t1", "t2", "t3", "t4", "t5"]


def test_validator_preserves_order_within_cap():
    """Order should be first-seen — important so the most-relevant tag
    (the LLM's first pick) is retained when capping kicks in."""
    from common.enrichment.schema import EnrichmentResult

    r = EnrichmentResult(tags=["first", "second", "first", "third"])
    # Dedupe keeps first occurrence; order preserved.
    assert r.tags == ["first", "second", "third"]


def test_validator_handles_empty_list():
    from common.enrichment.schema import EnrichmentResult

    r = EnrichmentResult(tags=[])
    assert r.tags == []


def test_validator_handles_non_string_entries_defensively():
    """LLM occasionally returns numbers or null; coerce to string then
    fall through normalisation. Garbage in, empty out (rather than 500)."""
    from common.enrichment.schema import EnrichmentResult

    r = EnrichmentResult(tags=["meeting", None, 42, "decision"])  # type: ignore[list-item]
    # None drops; 42 → "42" then normalised; meeting/decision pass through.
    assert "meeting" in r.tags
    assert "decision" in r.tags
    assert "42" in r.tags
    assert None not in r.tags
