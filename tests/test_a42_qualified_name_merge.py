"""A42 — two distinct entities distinguished by a non-digit qualifier merged.

``_same_identifier_signature`` compared only DIGIT-BEARING tokens, so
'acme (delaware)' and 'acme (ohio)' both produced an empty set, compared equal,
and were allowed to merge at cosine >= 0.85. Downstream that reads as
same_subject=true and yields a false contradiction between two different things.

The guard now also compares bracketed qualifiers — matching the discriminator
vocabulary ``_reattach_subject_discriminators`` already uses — and blocks a merge
only when two PRESENT qualifiers conflict.
"""

import pytest

from core_api.services.entity_extraction_worker import (
    _qualifier_signature,
    _same_identifier_signature,
)

pytestmark = pytest.mark.unit


# ── the bug ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "a,b",
    [
        ("acme (delaware)", "acme (ohio)"),
        ("priya (acmecorp)", "priya (betaindustries)"),
        ("john smith (legal)", "john smith (engineering)"),
        ("acme [emea]", "acme [apac]"),
    ],
)
def test_conflicting_qualifiers_block_the_merge(a, b):
    assert _same_identifier_signature(a, b) is False


# ── what must NOT regress ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "a,b",
    [
        ("acme", "acme corp"),
        ("acme corp", "acme corporation"),
        ("anna bergstrom", "anna bergström"),
        ("acme (ohio)", "acme (ohio)"),
        ("acme (Ohio)", "acme ( ohio )"),  # normalisation: case + whitespace
    ],
)
def test_legitimate_merges_still_allowed(a, b):
    """Fragmenting normal aliases is a worse failure than an over-merge: an
    over-merge is visible and recoverable, an entity that never coalesces is not."""
    assert _same_identifier_signature(a, b) is True


def test_an_absent_qualifier_is_unspecified_not_different():
    """Deliberate asymmetry — blocking this would strand every qualified mention
    from its own plain surface form."""
    assert _same_identifier_signature("acme", "acme (ohio)") is True
    assert _same_identifier_signature("acme (ohio)", "acme") is True


def test_the_original_digit_guard_still_holds():
    """A42 must not weaken the case the guard was built for."""
    assert _same_identifier_signature("comet #0002", "comet #0012") is False
    assert _same_identifier_signature("comet #0002", "comet #0002") is True


def test_qualifier_extraction():
    assert _qualifier_signature("acme (delaware)") == frozenset({"delaware"})
    assert _qualifier_signature("acme [EMEA] (legal)") == frozenset({"emea", "legal"})
    assert _qualifier_signature("acme") == frozenset()
    assert (
        _qualifier_signature("acme ()") == frozenset()
    )  # empty bracket is not a qualifier


def test_digits_and_qualifiers_are_both_required_to_agree():
    """A matching qualifier does not excuse a differing identifier."""
    assert (
        _same_identifier_signature("comet #0002 (emea)", "comet #0012 (emea)") is False
    )
