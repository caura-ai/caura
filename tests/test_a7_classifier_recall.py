"""A7 — query classifier mis-routes entity-token queries.

Gap measured: strategy accuracy 0.39, entity_lookup recall 0.20.
Entity-shaped queries fall through to semantic search instead of
the FTS/entity-lookup short-circuit. Two root causes addressed
here:

1. **Acronym entities are dropped before they reach FTS.** The
   tokenizer's ``ENTITY_TOKEN_MIN_LENGTH = 3`` floor silently
   discards ``AI``, ``ML``, ``PR``, ``UI``, ``QA``, ``HR``,
   ``UK``, ``US`` — all common 2-char entity names in real
   queries. Lowered to 2 (still floored by the stopword filter,
   which catches noisy 2-char words like ``in`` / ``on`` /
   ``to`` / ``be`` / ``is``).

2. **Storage FTS ANDs tokens.** ``plainto_tsquery('english', "a b
   c")`` becomes ``a & b & c`` — matches only entities containing
   ALL terms. For a query like "Helios telescope status",
   tokens ``[helios, telescope, status]`` (status is stopword;
   leaves ``[helios, telescope]``) AND'd misses the entity
   ``Helios Robotics`` (no telescope in name). Switching to OR
   matches on any token; the downstream graph expansion + memory
   linking + ``GRAPH_MAX_EXPANDED_ENTITIES`` cap are the natural
   precision filter.

Conservative trade-offs:
- Lower min_length increases the candidate token set; the stopword
  list catches most noise.
- OR over-matches entities but the downstream linking step's bounded
  fan-out keeps top-K stable.
"""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Tokenizer — acronym retention and 2-char stopword coverage.
# ---------------------------------------------------------------------------


def test_two_char_acronyms_kept():
    """Common 2-char acronym entities reach FTS."""
    from core_api.services.entity_tokens import extract_entity_tokens

    out = extract_entity_tokens("AI roadmap")
    assert "ai" in out, out


def test_two_char_acronym_ml_kept():
    from core_api.services.entity_tokens import extract_entity_tokens

    assert "ml" in extract_entity_tokens("ML pipeline ownership")


def test_two_char_acronym_pr_kept():
    from core_api.services.entity_tokens import extract_entity_tokens

    assert "pr" in extract_entity_tokens("PR review queue")


def test_two_char_country_codes_kept():
    from core_api.services.entity_tokens import extract_entity_tokens

    # ``UK`` is sometimes an entity (UK office, UK team, etc.)
    out = extract_entity_tokens("UK office expansion")
    assert "uk" in out


def test_single_char_still_dropped():
    """``X``, ``A`` etc. are below the new floor of 2 → still dropped.
    Keeps the tokenizer from emitting single-letter noise."""
    from core_api.services.entity_tokens import extract_entity_tokens

    out = extract_entity_tokens("X reviewed the PR")
    assert "x" not in out
    assert "pr" in out  # acronym retention check


def test_two_char_english_stopwords_still_dropped():
    """Common 2-char English words remain in the stopword list and
    are dropped even with the new min-length floor."""
    from core_api.services.entity_tokens import extract_entity_tokens

    # ``in`` ``on`` ``to`` ``be`` ``is`` ``no`` ``of`` ``by``
    out = extract_entity_tokens("AI is in the news on TV today")
    # Surviving tokens: ai, news, tv. All others stopwords.
    assert out == ["ai", "news", "tv"], out


def test_motivating_acronym_queries_now_yield_tokens():
    """Real-world entity queries that previously emitted ZERO tokens
    (because all surviving content was 2-char acronyms) now emit
    actionable tokens for FTS. ``status`` is already in
    ENTITY_STOPWORDS so it never reaches FTS; the point is ``ai``
    and ``qa`` (the acronyms) do."""
    from core_api.services.entity_tokens import extract_entity_tokens

    assert extract_entity_tokens("Who owns AI?") == ["owns", "ai"]
    # ``status`` is a stopword; possessive ``'s`` stripped → ``qa`` remains.
    assert extract_entity_tokens("What is QA's status?") == ["qa"]


# ---------------------------------------------------------------------------
# Storage FTS — OR-match across tokens instead of AND.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_entity_fts_or_match_returns_partial_overlap(sc):
    """Tokens A, B sent to FTS; entity containing only A is returned.
    With the previous AND behaviour the entity would have been hidden.
    """
    from uuid import uuid4

    tenant = f"test-tenant-a7-or-{uuid4().hex[:8]}"
    # Entity with canonical_name containing only "helios" (no "telescope").
    helios = await sc.create_entity(
        {
            "tenant_id": tenant,
            "entity_type": "organization",
            "canonical_name": "helios robotics",
        }
    )
    # Tokens [helios, telescope] — old AND would miss; OR finds Helios.
    result = await sc.fts_search_entities(
        {
            "tenant_id": tenant,
            "tokens": ["helios", "telescope"],
        }
    )
    assert helios["id"] in result, (
        f"Expected helios entity {helios['id']} in OR-match result; got {result}"
    )


@pytest.mark.integration
async def test_entity_fts_or_match_unions_per_token_matches(sc):
    """Two entities, each matching only one token; OR returns both."""
    from uuid import uuid4

    tenant = f"test-tenant-a7-union-{uuid4().hex[:8]}"
    e_alpha = await sc.create_entity(
        {
            "tenant_id": tenant,
            "entity_type": "organization",
            "canonical_name": "alpha corp",
        }
    )
    e_beta = await sc.create_entity(
        {
            "tenant_id": tenant,
            "entity_type": "organization",
            "canonical_name": "beta inc",
        }
    )
    result = await sc.fts_search_entities(
        {
            "tenant_id": tenant,
            "tokens": ["alpha", "beta"],
        }
    )
    assert e_alpha["id"] in result
    assert e_beta["id"] in result


@pytest.mark.integration
async def test_entity_fts_single_token_unchanged_behavior(sc):
    """Single-token queries still work — the AND→OR change is a no-op
    when there's only one term."""
    from uuid import uuid4

    tenant = f"test-tenant-a7-single-{uuid4().hex[:8]}"
    e = await sc.create_entity(
        {
            "tenant_id": tenant,
            "entity_type": "organization",
            "canonical_name": "vermillion seven",
        }
    )
    result = await sc.fts_search_entities(
        {
            "tenant_id": tenant,
            "tokens": ["vermillion"],
        }
    )
    assert e["id"] in result


@pytest.mark.integration
async def test_entity_fts_empty_tokens_returns_empty(sc):
    """Defensive: empty token list → empty result, not a 500."""
    from uuid import uuid4

    tenant = f"test-tenant-a7-empty-{uuid4().hex[:8]}"
    result = await sc.fts_search_entities(
        {
            "tenant_id": tenant,
            "tokens": [],
        }
    )
    assert result == []
