"""Three low-severity ingest findings.

oss-0902-l-37: ``len(body.split()) < 5`` dropped every fact written in a script
  that does not put spaces between words. A whole Chinese or Japanese sentence
  is one whitespace token, so the fragment filter deleted exactly the content it
  exists to protect — and only for those languages.

oss-0814-l-28: the doc-hash cache-hit branch omitted ``doc_hash`` (and
  ``sections``) from its response. The response's own docstring tells callers to
  echo ``doc_hash`` to commit so the NEXT preview can hit the cache, so a client
  following the documented flow lost the hash precisely when the cache was
  working.

oss-0814-l-49: ``_INGEST_MAX_CONTENT_CHARS`` was defined, never read, and
  carried a comment describing truncation ("sets ``truncated: true`` +
  ``original_length``") that the function's own docstring contradicts ("no
  longer truncated post-PR#7"). Dead code that documents behaviour the service
  does not have is worse than no comment.
"""

import inspect

import pytest

from core_api.services import ingest_service as ing

pytestmark = pytest.mark.unit


# ── oss-0902-l-37 ─────────────────────────────────────────────────────────


def test_english_counting_is_unchanged():
    """The fix must not move the boundary for space-delimited text."""
    assert ing._fact_word_count("Acme raised its revenue target") == 5
    assert ing._fact_word_count("Quarterly revenue rose") == 3


def test_a_cjk_sentence_is_no_longer_one_word():
    """The defect in one assertion: this is a real fact, and a whitespace split
    calls it a one-word fragment."""
    jp = "売上は前年比で12パーセント増加した"
    assert len(jp.split()) == 1
    assert ing._fact_word_count(jp) >= ing._MIN_FACT_WORDS


def test_mixed_script_counts_each_half_once():
    """A latin token inside CJK text must not be counted twice — once as a
    whitespace word and again as characters."""
    assert ing._fact_word_count("Acme の売上は増加した") == 1 + len("の売上は増加した")


def test_hangul_is_excluded_because_korean_uses_spaces():
    """Counting each syllable would over-count Korean and let genuine fragments
    through — the opposite failure."""
    ko = "제목"  # a two-syllable heading: still a fragment
    assert ing._fact_word_count(ko) == 1


def test_a_short_cjk_fragment_is_still_dropped():
    """The filter must keep working for the case it was written for."""
    assert ing._fact_word_count("概要") < ing._MIN_FACT_WORDS


def test_the_validator_uses_the_script_aware_count():
    """A correct helper is worth nothing if the call site still splits on
    whitespace."""
    src = inspect.getsource(ing)
    assert "_fact_word_count(body) < _MIN_FACT_WORDS" in src
    assert "len(body.split()) < _MIN_FACT_WORDS" not in src


# ── oss-0814-l-28 ─────────────────────────────────────────────────────────


def _cache_branch() -> str:
    src = inspect.getsource(ing)
    i = src.index("if cached_memories:")
    return src[i : src.index("whitespace / too-short", i)]


def test_cache_hit_returns_the_doc_hash_it_tells_callers_to_echo():
    assert '"doc_hash": doc_hash,' in _cache_branch()


def test_cache_hit_reports_zero_sections_rather_than_omitting_it():
    """Absent reads as "unknown"; zero is the true number of LLM calls made on a
    cache hit, for the same reason ``chunk_ms`` is 0."""
    b = _cache_branch()
    assert '"sections": 0,' in b
    assert '"chunk_ms": 0,' in b


def test_both_preview_returns_carry_the_cache_contract_keys():
    """The two exits must agree, or the contract depends on which branch ran."""
    src = inspect.getsource(ing.ingest_preview)
    assert src.count('"doc_hash"') >= 2
    assert src.count('"sections"') >= 2


# ── oss-0814-l-49 ─────────────────────────────────────────────────────────


def test_the_dead_truncation_constant_is_gone():
    assert not hasattr(ing, "_INGEST_MAX_CONTENT_CHARS")


def test_no_comment_still_claims_truncation_that_does_not_happen():
    """The constant's comment promised ``truncated: true`` and
    ``original_length`` fields the service never sets."""
    src = inspect.getsource(ing)
    assert "truncated: true" not in src
    assert "original_length" not in src


def test_the_live_minimum_length_guard_is_untouched():
    """The neighbouring constant is real and load-bearing — deleting the wrong
    one would send trivial inputs to the LLM."""
    assert ing._INGEST_MIN_CONTENT_CHARS == 20
