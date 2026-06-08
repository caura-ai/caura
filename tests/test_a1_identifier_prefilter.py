"""A1 identifier pre-filter — skip semantic dedup when content carries
identifier-shaped tokens (UUIDs, PR refs, build numbers, version
strings, commit SHAs).

Why
───
The original A1 gap: ``X prefers tabs`` / ``X does not prefer tabs``
collapse to cosine ≥ 0.98 because the embedder treats the identifier
``X`` as a low-information template slot. Similarly, ``Build pr-123
deployed`` / ``Build pr-456 deployed`` sit at ≥ 0.97 — the surrounding
template dominates the embedding, the identifier disambiguates the
meaning but is washed out by the rest of the sentence.

The earlier A1 chain (#190-#194) added two-tier thresholds, a judge,
the subject preflight, and a review queue. None of those help when
the identifier IS the disambiguator: the LLM judge would still see
two near-identical templates and the subject preflight has nothing
to act on (the identifier isn't an entity).

A1 pre-filter sits BEFORE the SQL similarity query. If the content
carries any identifier-shaped token, ``CheckSemanticDuplicate``
returns SKIPPED — exact-hash dedup at the write path is the safety
net (literal duplicate content still 409s; anything else writes).

Conservative on false-positives (won't drop valid writes), aggressive
on false-negatives (may let through "Deploy of build-123 was
successful" + "Build-123 deployment succeeded" if exact-hash differs).
Acceptable trade-off — the cost of a false-reject in this band is
silent data loss; the cost of a false-accept is one extra row.
"""

from __future__ import annotations

import pytest

from core_api.services.dedup_identifier_filter import _content_is_identifier_bearing


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Positive detections — each identifier shape suppresses dedup.
# ---------------------------------------------------------------------------


def test_detects_uuid():
    assert _content_is_identifier_bearing(
        "Document 4e3d2b1a-5f89-4c3e-bf90-9a8e2d61f4ad shipped today."
    )


def test_detects_pr_hash_ref():
    assert _content_is_identifier_bearing("PR#1234 merged to main yesterday.")


def test_detects_pr_dash_ref():
    assert _content_is_identifier_bearing("Reviewed pr-456 for the migration.")


def test_detects_build_number_hyphen():
    assert _content_is_identifier_bearing("Build-789 deployed to staging.")


def test_detects_build_number_word():
    assert _content_is_identifier_bearing("Build 1024 deployed successfully.")


def test_detects_semver():
    assert _content_is_identifier_bearing("Released v2.6.1 to production.")


def test_detects_semver_with_rc():
    assert _content_is_identifier_bearing("Cut v1.2.0-rc4 last Friday.")


def test_detects_commit_sha_short():
    """Git short SHAs are 7-12 hex chars."""
    assert _content_is_identifier_bearing(
        "Reverted commit a1b2c3d to fix the regression."
    )


def test_detects_commit_sha_long():
    """Full 40-char git SHAs."""
    assert _content_is_identifier_bearing(
        "Picked sha 8f3c91a7e2b56d94f3a0b8e1c9d7f205634a2cde for the patch."
    )


def test_detects_ticket_ref():
    """JIRA-style ticket refs (PROJECT-123)."""
    assert _content_is_identifier_bearing("CAURA-679 was resolved.")


def test_detects_hash_prefixed():
    """``sha256:<hex>`` content-addressed identifiers."""
    assert _content_is_identifier_bearing(
        "Image sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 deployed."
    )


# ---------------------------------------------------------------------------
# Negative detections — pure natural text passes through to dedup.
# ---------------------------------------------------------------------------


def test_no_match_on_plain_text():
    assert not _content_is_identifier_bearing(
        "Priya Sharma joined the platform team in Berlin last quarter."
    )


def test_no_match_on_short_numbers():
    """Plain dates / counts / years are NOT identifiers."""
    assert not _content_is_identifier_bearing(
        "The 2024 review meeting had 12 attendees from 3 teams."
    )


def test_no_match_on_decimal_in_prose():
    """``$1.99`` style prose numbers shouldn't trip the version regex."""
    assert not _content_is_identifier_bearing("The coffee costs $1.99 at the kiosk.")


def test_no_match_on_camelcase_word():
    """Plain CamelCase names (people, products) aren't identifiers."""
    assert not _content_is_identifier_bearing("Marcus Kowalski is a chief surgeon.")


def test_empty_content_is_not_identifier_bearing():
    assert not _content_is_identifier_bearing("")


def test_short_hex_words_dont_trip_the_sha_detector():
    """Words like ``café``, ``decade``, ``ace`` shouldn't look like
    short hex SHAs. The detector requires explicit context (``commit``,
    ``sha``, ``hash``) OR a length floor ≥ 7 hex-only chars NOT
    bracketed by letters."""
    assert not _content_is_identifier_bearing(
        "Decade-old café story about ace players."
    )


# ---------------------------------------------------------------------------
# Pipeline wiring — CheckSemanticDuplicate skips when identifier-bearing.
# ---------------------------------------------------------------------------


def _build_ctx(*, content: str):
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.tenant_config = MagicMock(semantic_dedup_enabled=True)
    ctx.data = {
        "input": MagicMock(
            tenant_id="t1",
            fleet_id="f1",
            agent_id="a1",
            visibility="scope_team",
            subject_entity_id=None,
            content=content,
        ),
        "embedding": [0.1] * 10,
        "memory_fields": {"metadata": {}},
    }
    return ctx


@pytest.mark.asyncio
async def test_check_semantic_duplicate_skips_when_identifier_bearing():
    """Identifier-bearing content → step returns SKIPPED before the
    SQL similarity query runs. No 409, no LLM, no review queue entry."""
    from unittest.mock import AsyncMock, patch

    from core_api.pipeline.step import StepOutcome
    from core_api.pipeline.steps.write.check_semantic_duplicate import (
        CheckSemanticDuplicate,
    )

    ctx = _build_ctx(content="Build pr-456 deployed to staging.")
    find = AsyncMock()
    judge = AsyncMock()
    enqueue = AsyncMock()
    with (
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=find,
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._llm_dedup_check",
            new=judge,
        ),
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._enqueue_dedup_review",
            new=enqueue,
        ),
    ):
        step = CheckSemanticDuplicate()
        result = await step.execute(ctx)

    assert result is not None
    assert result.outcome == StepOutcome.SKIPPED
    find.assert_not_called()
    judge.assert_not_called()
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_check_semantic_duplicate_proceeds_on_plain_content():
    """Plain natural-text content (no identifier tokens) → step
    proceeds to the SQL query as before. Pre-filter is opt-out, not
    opt-in: don't change behaviour for the common case."""
    from unittest.mock import AsyncMock, patch

    from core_api.pipeline.steps.write.check_semantic_duplicate import (
        CheckSemanticDuplicate,
    )

    ctx = _build_ctx(content="Priya joined the Berlin office last quarter.")
    find = AsyncMock(return_value=None)
    with (
        patch(
            "core_api.pipeline.steps.write.check_semantic_duplicate._find_semantic_duplicate",
            new=find,
        ),
    ):
        step = CheckSemanticDuplicate()
        await step.execute(ctx)

    find.assert_called_once()


# ---------------------------------------------------------------------------
# Failure mode that motivated this PR — verify the pre-filter catches it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pair_label, content",
    [
        ("Build-pr template", "Build pr-456 deployed to staging."),
        ("Build-pr template variant", "Build pr-789 deployed to staging."),
        ("Negation on identifier", "X#42 prefers tabs."),
        ("Negation variant", "X#42 does not prefer tabs."),
    ],
)
def test_motivating_failure_modes_caught(pair_label, content):
    """Each member of the original A1 failure-mode pairs is correctly
    classified as identifier-bearing — so the step short-circuits and
    the pair never reaches the LLM judge / auto-reject."""
    assert _content_is_identifier_bearing(content), (
        f"pre-filter must catch this content: {content!r}"
    )
