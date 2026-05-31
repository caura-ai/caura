"""A27 — FTS-weight gate and entity-FTS gate share the same tokenizer.

Before A27, ``_adaptive_fts_weight`` used naive ``query.split()`` while
``extract_entity_tokens`` (the entity-FTS gate) split on hyphens too.
A hyphenated identifier like ``claude-opus-4-7`` produced disagreeing
views — 1 token to the FTS-weight gate, 4 tokens to entity-FTS — so
the two gates routed inconsistently across the same query.

These tests prove the share is in place and the existing behaviour
the FTS-weight gate cared about (sigil handle / ticker boosts; raw
sentence-length detection) survives the migration.
"""

from __future__ import annotations

import pytest

from core_api.constants import FTS_WEIGHT, FTS_WEIGHT_BOOSTED
from core_api.services.entity_tokens import extract_entity_tokens
from core_api.services.memory_service import _adaptive_fts_weight

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# extract_entity_tokens — preserve_case kwarg.
# ---------------------------------------------------------------------------


class TestExtractEntityTokensPreserveCase:
    def test_default_lowercases(self):
        assert extract_entity_tokens("CertiK OpenAI") == ["certik", "openai"]

    def test_preserve_case_keeps_original(self):
        assert extract_entity_tokens("CertiK OpenAI", preserve_case=True) == [
            "CertiK",
            "OpenAI",
        ]

    def test_preserve_case_still_filters_stopwords(self):
        assert extract_entity_tokens("the OpenAI Foundation", preserve_case=True) == [
            "OpenAI",
            "Foundation",
        ]

    def test_preserve_case_still_drops_hex_ids(self):
        # 8+ hex chars are dropped regardless of case-preservation.
        out = extract_entity_tokens("abc123-deadbeef notable", preserve_case=True)
        assert "deadbeef" not in out
        assert "notable" in out


# ---------------------------------------------------------------------------
# Token agreement between gates.
# ---------------------------------------------------------------------------


class TestTokenizerAgreement:
    """Both gates should classify hyphenated identifiers the same way."""

    def test_hyphenated_identifier_does_not_overboost(self):
        # A27 root-cause case: claude-opus-4-7 was 1 specific token to
        # _adaptive_fts_weight (lone token with digits → BOOSTED) and 4
        # tokens to the entity-FTS gate. After A27, the FTS-weight gate
        # sees the same split — claude/opus/4/7 — and neither "claude"
        # nor "opus" are specific (no digits, no upper, no sigil), so
        # the query no longer auto-boosts.
        assert _adaptive_fts_weight("claude-opus-4-7") == FTS_WEIGHT

    def test_hyphenated_identifier_yields_multiple_entity_tokens(self):
        # Sanity check that the entity-FTS gate continues to see the
        # multiple-token view it always did. Both gates now agree on
        # boundary detection — that's the A27 contract.
        assert extract_entity_tokens("claude-opus-4-7") == ["claude", "opus"]


# ---------------------------------------------------------------------------
# Sigil preservation across the share.
# ---------------------------------------------------------------------------


class TestSigilPreservation:
    """Handle / ticker / hashtag tokens still boost FTS weight even though
    ``extract_entity_tokens`` strips the leading sigil before returning."""

    def test_handle_boosts(self):
        assert _adaptive_fts_weight("@karpathy") == FTS_WEIGHT_BOOSTED

    def test_ticker_boosts(self):
        assert _adaptive_fts_weight("$BTC") == FTS_WEIGHT_BOOSTED

    def test_hashtag_boosts(self):
        assert _adaptive_fts_weight("#trending") == FTS_WEIGHT_BOOSTED

    def test_sigil_with_other_content_still_boosts(self):
        # 'find $BTC price' — 'find' is a stopword, leaves $BTC + price.
        # The sigil + BTC's all-caps both count as specific.
        assert _adaptive_fts_weight("find $BTC price") == FTS_WEIGHT_BOOSTED


# ---------------------------------------------------------------------------
# Raw-count MAX_TOKENS gate (preserves natural-language detection).
# ---------------------------------------------------------------------------


class TestRawMaxTokensGate:
    """4+ raw words → semantic intent, default weight, even when the
    shared filter would collapse the query to a single meaningful token."""

    def test_long_sentence_with_one_specific_stays_default(self):
        # Pre-A27 would have stayed default because raw count > MAX. The
        # naive 'meaningful = [...]' filter inside _adaptive_fts_weight
        # left the same 1 specific over a small denominator and the raw
        # MAX gate is what kept the boost from firing. A27 preserves
        # the raw MAX gate explicitly.
        assert _adaptive_fts_weight("tell me about NEXAI") == FTS_WEIGHT

    def test_long_sentence_with_camelcase_stays_default(self):
        assert _adaptive_fts_weight("what is the address of OpenAI") == FTS_WEIGHT

    def test_long_sentence_with_ticker_stays_default(self):
        assert _adaptive_fts_weight("how much is $BTC worth now") == FTS_WEIGHT
