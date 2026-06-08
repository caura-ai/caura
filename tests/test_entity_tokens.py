"""Unit tests for ``core_api.services.entity_tokens.extract_entity_tokens``.

Mirrors the integration-level checks in ``tests/pipeline/test_classify_query.py``
but at the helper level so all three call sites that consume it
(``ClassifyQuery``, ``_entity_boost_pipeline``, ``ParallelEmbedEntityBoost``)
are covered by a single source of truth.
"""

from __future__ import annotations

from core_api.services.entity_tokens import extract_entity_tokens


def test_simple_query_unchanged():
    assert extract_entity_tokens("Alice and Bob") == ["alice", "bob"]


def test_drops_stopwords_and_short_tokens():
    # "the" is a stopword; "of" is too short.
    assert extract_entity_tokens("the home of Alice") == ["home", "alice"]


def test_internal_punctuation_splits():
    assert extract_entity_tokens("USA-based,research/scribe") == [
        "usa",
        "based",
        "research",
        "scribe",
    ]


def test_hex_only_fragments_dropped():
    hex_chunk = "deadbeefcafe1234deadbeefcafe1234"
    assert extract_entity_tokens(f"xyzzy-{hex_chunk}-FLIBBERTY") == [
        "xyzzy",
        "flibberty",
    ]


def test_short_hex_english_words_kept():
    """All-hex words below the 8-char floor are real entity names, not IDs."""
    assert extract_entity_tokens("cafe face dead beef decade facade") == [
        "cafe",
        "face",
        "dead",
        "beef",
        "decade",
        "facade",
    ]


def test_hyphenated_uuid_partially_filtered():
    """8/12-char UUID segments dropped; the three 4-char ones survive."""
    assert extract_entity_tokens("550e8400-e29b-41d4-a716-446655440000") == [
        "e29b",
        "41d4",
        "a716",
    ]


def test_tokens_are_lowercased():
    """Centralised normalisation: callers no longer need their own .lower()."""
    assert extract_entity_tokens("MixedCase QUERY tokens") == [
        "mixedcase",
        "query",
        "tokens",
    ]


def test_snake_case_identifiers_kept_intact():
    """Underscore is not a token separator: snake_case names stay whole."""
    assert extract_entity_tokens("project_alpha api_key") == [
        "project_alpha",
        "api_key",
    ]


def test_empty_query_yields_empty_list():
    assert extract_entity_tokens("") == []


def test_quotes_and_apostrophes_stripped_via_string_punctuation():
    # ``string.punctuation`` strip catches residual quotes the splitter
    # doesn't tokenise on (apostrophes, quotation marks).
    assert extract_entity_tokens("'Alice'") == ["alice"]
