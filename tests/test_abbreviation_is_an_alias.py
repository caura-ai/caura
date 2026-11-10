"""A correction written with an abbreviation the first fact defined.

    1: "The Ralenix Enterprise plan, or REP, carried a one-time setup fee of $880."
    2: "REP now carries a one-time setup fee of $1,120 for new subscriptions."

Fact 2 minted an entity ``rep`` beside ``ralenix enterprise plan``; with two
subjects, no contradiction path paired the facts (Fleet Memory Bench: 0.23 of
such corrections right vs 0.56 for plain ones). See
``core_api.services.abbreviations``.
"""

import pytest

from core_api.services.abbreviations import (
    defined_abbreviations,
    initials,
    looks_like_abbreviation,
)
from core_api.services.entity_extraction_worker import (
    _apply_defined_abbreviations,
    _resolve_abbreviations,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "content,name,expected",
    [
        (
            "The Ralenix Enterprise plan, or REP, carried a fee.",
            "ralenix enterprise plan",
            "rep",
        ),
        (
            "The Mollix coastal territory, internally called MCT, belonged to Sela Dorn.",
            "mollix coastal territory",
            "mct",
        ),
        (
            "The Quillhaven response desk (QRD) assigns escalations.",
            "quillhaven response desk (qrd)",
            "qrd",
        ),
        (
            "For Talvori, the designated customer liaison, or DCL, was Kori.",
            "talvori designated customer liaison",
            "dcl",
        ),
    ],
)
def test_definitions_are_found(content, name, expected):
    found = defined_abbreviations(content, [name])
    assert found[name][1] == expected
    assert "(" not in found[name][0]


def test_a_bracketed_qualifier_is_not_an_abbreviation():
    """A67's qualifiers stay: DE is not the initials of zylo."""
    assert defined_abbreviations("Zylo (DE) filed in March.", ["zylo (de)"]) == {}
    assert defined_abbreviations("Acme (Delaware) filed.", ["acme (delaware)"]) == {}


def test_initials_skip_function_words():
    assert initials("bank of the north") == "bn"


def test_bare_abbreviation_needs_capitals_in_the_content():
    assert looks_like_abbreviation("rep", "REP now carries a fee.")
    assert not looks_like_abbreviation("rep", "The sales rep left.")
    assert not looks_like_abbreviation("ralenix", "RALENIXES")


def test_apply_folds_a_bare_abbreviation_into_the_full_name():
    content = (
        "The Quillhaven response desk (QRD) assigns escalations to the Eastmarsh rota."
    )
    filtered = [
        ("qrd", "organization", "subject"),
        ("quillhaven response desk (qrd)", "organization", "mentioned"),
        ("eastmarsh rota", "concept", "object"),
    ]
    out, alias_of = _apply_defined_abbreviations(content, filtered)
    assert out == [
        ("quillhaven response desk", "organization", "subject"),
        ("eastmarsh rota", "concept", "object"),
    ]
    assert alias_of == {"quillhaven response desk": "qrd"}


def test_apply_leaves_other_memories_alone():
    filtered = [("billing service", "technology", "subject")]
    assert _apply_defined_abbreviations(
        "The billing service runs nightly.", filtered
    ) == (filtered, {})


class _Storage:
    def __init__(self, rows):
        self.rows = rows

    async def fts_search_entities(self, data):
        return list(self.rows)

    async def get_entities_by_ids(self, ids, tenant_id):
        return {i: self.rows[i] for i in ids}


async def test_bare_abbreviation_resolves_to_the_aliased_entity():
    sc = _Storage(
        {
            "e1": {
                "canonical_name": "ralenix enterprise plan",
                "attributes": {"_aliases": ["ralenix enterprise plan", "rep"]},
            },
            "e2": {
                "canonical_name": "sales rep",
                "attributes": {"_aliases": ["sales rep"]},
            },
        }
    )
    filtered = [("rep", "concept", "subject")]
    resolved = [None]
    await _resolve_abbreviations(
        sc, "t", "f", "REP now carries a fee.", filtered, resolved
    )
    assert resolved == [
        {"entity_id": "e1", "canonical_name": "ralenix enterprise plan"}
    ]


async def test_two_candidates_resolve_nothing():
    alias = {"_aliases": ["rep"]}
    sc = _Storage(
        {
            "e1": {"canonical_name": "ralenix enterprise plan", "attributes": alias},
            "e2": {"canonical_name": "regional expansion program", "attributes": alias},
        }
    )
    resolved = [None]
    await _resolve_abbreviations(
        sc,
        "t",
        "f",
        "REP now carries a fee.",
        [("rep", "concept", "subject")],
        resolved,
    )
    assert resolved == [None]
