"""Tokenizer for entity FTS lookups.

Shared by every call site that feeds ``fts_search_entities``: the
``ClassifyQuery`` pipeline step, ``_entity_boost_pipeline`` in
``memory_service``, and the ``ParallelEmbedEntityBoost`` pipeline step.
Keeping one tokenizer means a single place to harden against
UUID-fragment / content-hash leaks into the entity FTS index — the
``empty-query-nonempty`` loadtest finding came from each site
calling ``query.split()`` independently and sending unbroken
hyphenated strings to storage, which then re-tokenised server-side
on punctuation and matched UUID chunks against indexed entities.
"""

from __future__ import annotations

import re
import string

from core_api.constants import ENTITY_STOPWORDS, ENTITY_TOKEN_MIN_LENGTH

# Internal punctuation splits before storage-side FTS sees the string.
# ``_`` is intentionally excluded from the separator class so snake_case
# entity names (``project_alpha``, ``api_key``) reach FTS as a single
# token; UUIDs use hyphens, which is what the empty-query-nonempty
# loadtest probe surfaced.
_TOKEN_SEP_RE = re.compile(r"[\s\-/.,;:!?()\[\]{}]+")
_HEX_ONLY_RE = re.compile(r"[0-9a-f]+", re.IGNORECASE)
_HEX_ID_MIN_LENGTH = 8


def _is_hex_id(s: str) -> bool:
    """True for UUID segments / content hashes / oid-style IDs.

    The 8-char floor sits above the longest common all-hex English word
    (~6 chars: ``decade``, ``facade``), so shorter all-hex words like
    ``cafe`` / ``face`` / ``dead`` / ``beef`` are kept. 4-char UUID
    segments (``e29b``, ``41d4`` …) also survive — an acceptable
    trade-off, since they won't match real entity names downstream.
    """
    return len(s) >= _HEX_ID_MIN_LENGTH and bool(_HEX_ONLY_RE.fullmatch(s))


# ASCII apostrophe + U+2019 smart-quote possessive. Smart-quote
# built via chr() so the source file stays pure-ASCII (ruff RUF001
# flags raw smart quotes as ambiguous prose).
_POSSESSIVE_SUFFIXES = ("'s", chr(0x2019) + "s")


def _strip_possessive(s: str) -> str:
    """Strip trailing possessive ``'s`` (ASCII apostrophe or
    smart-quote U+2019) so ``QA's`` becomes ``QA``. The tokenizer's
    separator class deliberately keeps apostrophes internal (to avoid
    splitting contractions into noise like ``don`` / ``won``), but
    possessives carry no entity-FTS signal."""
    for suffix in _POSSESSIVE_SUFFIXES:
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)]
    return s


def extract_entity_tokens(query: str) -> list[str]:
    """Return entity-FTS-ready tokens from ``query``.

    Splits on whitespace and internal punctuation; drops short tokens,
    stopwords, and hex-only IDs; strips possessive ``'s``; lowercases
    each surviving token so callers don't have to.
    """
    return [
        lower
        for t in _TOKEN_SEP_RE.split(query)
        if (stripped := _strip_possessive(t.strip(string.punctuation)))
        and len(stripped) >= ENTITY_TOKEN_MIN_LENGTH
        and (lower := stripped.lower()) not in ENTITY_STOPWORDS
        and not _is_hex_id(stripped)
    ]
