"""Unit tests for ``core_api.services.doc_indexing.resolve_embed_source``.

Single source of truth shared by the MCP handler and the REST route, so
the contract is asserted here once instead of in both handler tests.
"""

from __future__ import annotations

import pytest

from core_api.services.doc_indexing import (
    InvalidDocIndexingError,
    resolve_embed_source,
)


# ---------------------------------------------------------------------------
# Non-skills collections: summary optional
# ---------------------------------------------------------------------------


def test_non_skills_no_summary_returns_none():
    """No ``summary`` in data → no embedding (doc stored unindexed)."""
    assert resolve_embed_source("customers", {"plan": "enterprise"}) is None


def test_non_skills_summary_present_returns_it():
    """Valid ``summary`` in data → that string is what gets embedded."""
    out = resolve_embed_source(
        "notes", {"summary": "Meeting notes for Q2 planning", "content": "..."}
    )
    assert out == "Meeting notes for Q2 planning"


def test_non_skills_empty_summary_raises():
    """Non-empty-string guard — an empty/whitespace summary would
    produce a noise embedding; fail loud rather than silently skip."""
    with pytest.raises(InvalidDocIndexingError, match="non-empty string"):
        resolve_embed_source("notes", {"summary": "   "})


def test_non_skills_non_string_summary_raises():
    """A non-string ``summary`` (number, dict, None-like) is a caller bug."""
    with pytest.raises(InvalidDocIndexingError, match="non-empty string"):
        resolve_embed_source("notes", {"summary": 42})


# ---------------------------------------------------------------------------
# Skills collection: summary OR description required, summary preferred
# ---------------------------------------------------------------------------


def test_skills_with_summary_returns_summary():
    out = resolve_embed_source(
        "skills",
        {
            "summary": "Refactor recipe: extract method",
            "description": "ignored when summary present",
        },
    )
    assert out == "Refactor recipe: extract method"


def test_skills_summary_wins_over_description():
    """Both fields present → server prefers summary; description is a
    fallback for back-compat callers that haven't migrated yet."""
    out = resolve_embed_source(
        "skills",
        {"summary": "winner", "description": "loser"},
    )
    assert out == "winner"


def test_skills_with_description_back_compat():
    """Old-style skills writes (description only, no summary) still
    index correctly via the back-compat fallback."""
    out = resolve_embed_source(
        "skills",
        {"name": "my-skill", "description": "Back-compat path."},
    )
    assert out == "Back-compat path."


def test_skills_missing_both_raises():
    """Skills catalog discoverability requires indexed text. Missing
    both summary AND description → 422 (raised here as ValueError)."""
    with pytest.raises(InvalidDocIndexingError, match="requires"):
        resolve_embed_source("skills", {"name": "my-skill", "content": "# x"})


def test_skills_empty_summary_falls_back_to_description():
    """An empty/whitespace summary is treated as absent — description
    fills in via back-compat. (Mirrors the non-skills empty-summary
    rejection: we don't want an empty summary to win over a valid
    description.)"""
    out = resolve_embed_source(
        "skills",
        {"summary": "   ", "description": "Real text."},
    )
    assert out == "Real text."


def test_skills_empty_summary_and_empty_description_raises():
    """Both fields present but both empty/whitespace → still 422."""
    with pytest.raises(InvalidDocIndexingError, match="requires"):
        resolve_embed_source(
            "skills",
            {"summary": "   ", "description": "  "},
        )
