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
   **RETIRED by CAURA-719** — the "downstream features that join on
   tags" this was tuned for never shipped, so the LLM is no longer
   asked for tags at all. See ``test_prompt_no_longer_requests_tags``.

2. **Defensive schema validator** — even with a tightened prompt the
   LLM drifts. ``EnrichmentResult.tags`` now passes through a
   normaliser that lowercases, strips whitespace, replaces internal
   whitespace / underscores with hyphens, dedupes, drops empties,
   and caps at 5. The set the downstream tag-join sees is now
   stable regardless of the LLM's exact spelling.
   **STILL LIVE after CAURA-719** — tags became a caller-owned key
   (C25 ``CALLER_OWNABLE_KEYS``), so the normaliser now guards
   caller-supplied and historical values rather than LLM output. Every
   validator test below therefore still applies.

Conservative on singularization: English -s heuristic breaks
``news`` / ``sales`` / ``headquarters`` etc., so the validator
does NOT singularize. It only normalizes spacing/case.
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


def test_prompt_no_longer_requests_tags():
    """CAURA-719 — A8's prompt-tightening half is RETIRED.

    A8 tuned tag generation against a ``mean_jaccard`` target for the
    benefit of "downstream features that join on tags". No such consumer
    was ever built: nothing filters, queries, or FTS-indexes tags (the
    search vector is ``title || content``, migration 034), so the whole
    field was write-only. Tags are now caller-owned (C25
    ``CALLER_OWNABLE_KEYS``) and the LLM is not asked for them.

    The validator half of A8 survives — see the section below — because
    caller-supplied and historical tag values still need normalising.
    """
    prompt = _get_prompt()
    assert '"tags"' not in prompt, "the enrichment prompt must not request tags"
    # The A8 guidance that only existed to shape tag output goes with it.
    for phrase in ("singular", "kebab", "post-mortem"):
        assert phrase not in prompt.lower(), (
            f"leftover A8 tag guidance in prompt: {phrase!r}"
        )


def test_prompt_word_count_remains_bounded():
    """Adding tag guidance must not bust the prompt budget. Ceiling
    raised to 1200 in A9 to cover the action/episode disambiguation
    block + A8's tag guidance. Raised again to 1500 in CAURA-701 for
    the V2.1 3-way action/episode/fact contrastive block. Raised to
    1800 in CAURA-717 for the V2.2 action-vs-decision resolver +
    expanded contrastive block (net-add after dropping 3 deprecated
    bullets).
    the V2.1 3-way action/episode/fact contrastive block."""
    prompt = _get_prompt()
    word_count = len(prompt.split())
    assert word_count < 1800, f"prompt is {word_count} words — too long"


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
