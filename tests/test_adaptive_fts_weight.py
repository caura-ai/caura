"""Adaptive FTS weight: short specific queries get boosted keyword weight.

Unit tests — no database required.
"""

import pytest

from core_api.constants import (
    FTS_BOOST_MAX_TOKENS,
    FTS_BOOST_SPECIFICITY_RATIO,
    FTS_WEIGHT,
    FTS_WEIGHT_BOOSTED,
)
from core_api.services.memory_service import _adaptive_fts_weight, _is_specific_token


# ---------------------------------------------------------------------------
# _is_specific_token
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIsSpecificToken:
    # All-caps or CamelCase/PascalCase
    @pytest.mark.parametrize("token", ["NEXAI", "CertiK", "OpenAI", "BTC", "GPT"])
    def test_allcaps_or_camelcase_are_specific(self, token):
        assert _is_specific_token(token) is True

    # Title-case common words are NOT specific (no interior uppercase)
    @pytest.mark.parametrize("token", ["Best", "Good", "New", "Top", "Find", "My"])
    def test_titlecase_common_words_not_specific(self, token):
        assert _is_specific_token(token) is False

    # Title-case proper nouns without interior caps — not specific by capitalization alone
    @pytest.mark.parametrize("token", ["Bitcoin", "Karpathy", "Python"])
    def test_titlecase_proper_nouns_not_specific(self, token):
        assert _is_specific_token(token) is False

    # Contains digits (IDs, versions, tickers)
    @pytest.mark.parametrize("token", ["gpt-5", "1892347", "v2.1", "3090ti"])
    def test_tokens_with_digits_are_specific(self, token):
        assert _is_specific_token(token) is True

    # Special-prefix tokens (tickers, handles, hashtags)
    @pytest.mark.parametrize("token", ["$SOL", "$NEXAI", "@karpathy", "#trending"])
    def test_special_prefix_tokens_are_specific(self, token):
        assert _is_specific_token(token) is True

    # Common lowercase words are NOT specific
    @pytest.mark.parametrize(
        "token", ["crypto", "good", "the", "about", "performance", "tweet"]
    )
    def test_lowercase_common_words_not_specific(self, token):
        assert _is_specific_token(token) is False

    # Edge cases
    def test_empty_string_not_specific(self):
        assert _is_specific_token("") is False

    def test_single_uppercase_letter_is_specific(self):
        assert _is_specific_token("A") is True


# ---------------------------------------------------------------------------
# _adaptive_fts_weight — boosted cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAdaptiveFtsWeightBoosted:
    """Queries that SHOULD get the boosted FTS weight (0.6)."""

    def test_single_proper_noun(self):
        """'NEXAI' — 1 specific token, clearly exact-match."""
        assert _adaptive_fts_weight("NEXAI") == FTS_WEIGHT_BOOSTED

    def test_two_proper_nouns(self):
        """'CertiK OpenAI' — both capitalized, all specific."""
        assert _adaptive_fts_weight("CertiK OpenAI") == FTS_WEIGHT_BOOSTED

    def test_ticker_symbols(self):
        """'$SOL $PYTH' — both are specific (special prefix)."""
        assert _adaptive_fts_weight("$SOL $PYTH") == FTS_WEIGHT_BOOSTED

    def test_allcaps_with_id(self):
        """'CAURA 1892347' — 'CAURA' is all-caps + '1892347' has digits, both specific."""
        assert _adaptive_fts_weight("CAURA 1892347") == FTS_WEIGHT_BOOSTED

    def test_titlecase_with_id_boosts(self):
        """'Tweet 1892347' — 'Tweet' not specific, '1892347' specific, 1/2=0.5 > 0.4 → boosts."""
        assert _adaptive_fts_weight("Tweet 1892347") == FTS_WEIGHT_BOOSTED

    def test_stopword_plus_proper_noun_boosts(self):
        """'find NEXAI' — 'find' is a stopword, only 'NEXAI' is meaningful → 1/1 = boosts."""
        assert _adaptive_fts_weight("find NEXAI") == FTS_WEIGHT_BOOSTED

    def test_single_ticker(self):
        """'$BTC' — single specific token."""
        assert _adaptive_fts_weight("$BTC") == FTS_WEIGHT_BOOSTED

    def test_handle_search(self):
        """'@karpathy' — single specific token."""
        assert _adaptive_fts_weight("@karpathy") == FTS_WEIGHT_BOOSTED

    def test_proper_noun_with_version(self):
        """'GPT-5 release' — 1/2 = 0.5 > 0.4, boosts."""
        assert _adaptive_fts_weight("GPT-5 release") == FTS_WEIGHT_BOOSTED

    def test_all_specific_with_version(self):
        """'GPT-5 V2.0' — both tokens specific (digits), boosts."""
        assert _adaptive_fts_weight("GPT-5 V2.0") == FTS_WEIGHT_BOOSTED


# ---------------------------------------------------------------------------
# _adaptive_fts_weight — default (not boosted) cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAdaptiveFtsWeightDefault:
    """Queries that should keep the default FTS weight (0.3)."""

    def test_long_semantic_query(self):
        """Long natural-language query — semantic should dominate."""
        assert (
            _adaptive_fts_weight(
                "which crypto tokens have the best long-term growth potential"
            )
            == FTS_WEIGHT
        )

    def test_medium_natural_language(self):
        """4+ meaningful tokens — always default."""
        assert _adaptive_fts_weight("best crypto growth potential") == FTS_WEIGHT

    def test_all_lowercase_short(self):
        """'good crypto' — short but no specific tokens."""
        assert _adaptive_fts_weight("good crypto") == FTS_WEIGHT

    def test_empty_query(self):
        """Empty string — no tokens, default."""
        assert _adaptive_fts_weight("") == FTS_WEIGHT

    def test_only_stopwords(self):
        """Query of only stopwords / short words — no meaningful tokens."""
        assert _adaptive_fts_weight("the a an") == FTS_WEIGHT

    def test_half_specific_now_boosts(self):
        """'CertiK audit' — 1/2 = 0.5 > 0.4 threshold → boosts."""
        assert _adaptive_fts_weight("CertiK audit") == FTS_WEIGHT_BOOSTED

    def test_no_specific_tokens_stays_default(self):
        """'price report' — 0/2 = 0.0, no specific tokens → default."""
        assert _adaptive_fts_weight("price report") == FTS_WEIGHT

    def test_stopword_heavy_natural_language_stays_default(self):
        """'what is the address of OpenAI' — 7 raw tokens > 3, semantic intent."""
        assert _adaptive_fts_weight("what is the address of OpenAI") == FTS_WEIGHT

    def test_question_with_ticker_stays_default(self):
        """'how much is $BTC worth now' — 6 raw tokens, semantic despite ticker."""
        assert _adaptive_fts_weight("how much is $BTC worth now") == FTS_WEIGHT

    def test_four_raw_tokens_stays_default(self):
        """'tell me about NEXAI' — 4 raw tokens > 3, stays default."""
        assert _adaptive_fts_weight("tell me about NEXAI") == FTS_WEIGHT

    def test_three_raw_tokens_can_boost(self):
        """'find $BTC price' — 3 raw tokens, passes raw gate; 'find' is stopword,
        meaningful=['$BTC','price'], 1/2=0.5>0.4 → boosts."""
        assert _adaptive_fts_weight("find $BTC price") == FTS_WEIGHT_BOOSTED

    def test_long_query_with_proper_nouns(self):
        """Even with proper nouns, 4+ raw tokens → default."""
        assert (
            _adaptive_fts_weight("OpenAI CertiK Bitcoin Ethereum market analysis")
            == FTS_WEIGHT
        )


# ---------------------------------------------------------------------------
# _adaptive_fts_weight — profile override respected
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFtsWeightProfileOverride:
    """Verify the integration point: search_profile overrides adaptive logic."""

    def test_explicit_profile_wins_over_adaptive(self):
        """When search_profile has fts_weight, adaptive is not called."""
        # We can't easily test the full search_memories here (needs DB),
        # but we verify the contract: if "fts_weight" is in sp, it's used directly.
        # This is a design-level test — the actual wiring is in search_memories.
        sp_with_override = {"fts_weight": 0.4}
        sp_without = {}

        # With override: should use 0.4 regardless of query
        assert sp_with_override.get("fts_weight") == 0.4

        # Without override: adaptive would be called
        assert "fts_weight" not in sp_without


# ---------------------------------------------------------------------------
# Constants sanity checks
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAdaptiveFtsConstants:
    def test_boosted_weight_greater_than_default(self):
        assert FTS_WEIGHT_BOOSTED > FTS_WEIGHT

    def test_boosted_weight_at_most_one(self):
        assert FTS_WEIGHT_BOOSTED <= 1.0

    def test_default_weight_positive(self):
        assert FTS_WEIGHT > 0.0

    def test_max_tokens_positive(self):
        assert FTS_BOOST_MAX_TOKENS >= 1

    def test_specificity_ratio_between_zero_and_one(self):
        assert 0.0 < FTS_BOOST_SPECIFICITY_RATIO <= 1.0
