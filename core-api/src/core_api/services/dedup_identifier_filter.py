"""A1 identifier pre-filter — detect content whose meaning is carried
by an identifier-shaped token (UUID, PR ref, build number, version
string, commit SHA, ticket ref).

For such content, semantic-similarity dedup is a false-positive
generator: the embedder treats the identifier as a low-information
template slot and collapses superficially different writes (different
build numbers, different PR refs, different versions) to cosine ≥
0.95. The earlier A1 chain's LLM judge has the same blind spot — it
sees two near-identical templates and rules them duplicates.

When the pre-filter fires, ``CheckSemanticDuplicate`` returns SKIPPED.
Exact-hash dedup at the write path is the only remaining guard:
literal duplicates still 409, everything else writes.

Conservative on false-positives (won't drop valid writes), aggressive
on false-negatives (may allow a few extra semantic duplicates that
happen to mention an identifier). The cost of a false-reject in this
band is silent data loss; the cost of a false-accept is one extra
row — the trade-off is correct.
"""

from __future__ import annotations

import re

# Each pattern matches a single token shape. None overlap with prose-
# common shapes (years, prices, decimal counts) — verified by the
# unit tests in ``tests/test_a1_identifier_prefilter.py``.
_IDENTIFIER_PATTERNS: tuple[re.Pattern[str], ...] = (
    # UUIDs (8-4-4-4-12 hex).
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
    # PR refs: ``PR#123``, ``pr-456``, ``#789`` (with surrounding text).
    re.compile(r"\b[Pp][Rr][-#]\d+\b"),
    # ``#nnnn`` (issue/PR shorthand) — require ≥ 3 digits to dodge ``#5``-style emphasis.
    re.compile(r"#\d{3,}\b"),
    # ``word#nnn`` — any non-digit word followed by ``#`` + digits.
    # This is unambiguously an identifier shape (issue, ticket, build,
    # internal ref) and covers the ``X#42`` motivating case where
    # only 2 digits follow the ``#``.
    re.compile(r"\b[A-Za-z]\w*#\d+\b"),
    # Build numbers: ``build-123``, ``build#123``, ``build 1024`` (case-insensitive).
    re.compile(r"\b[Bb]uild[-# ]\d+\b"),
    # Semver: ``v1.2.3`` / ``1.2.3`` / ``v2.0.0-rc4`` / ``2.0.0-rc4``. Requires
    # leading ``v`` OR a hyphen-suffixed pre-release tag so plain prose
    # decimals (``$1.99``) don't trip it.
    re.compile(r"\bv\d+\.\d+\.\d+(?:[-+][\w.]+)?\b"),
    re.compile(r"\b\d+\.\d+\.\d+[-+][\w.]+\b"),
    # Ticket refs: ``PROJECT-123`` (2+ uppercase letters + hyphen + digits).
    re.compile(r"\b[A-Z]{2,}-\d+\b"),
    # ``sha256:`` and similar content-addressed prefixes.
    re.compile(r"\b(?:sha\d*|md5):[0-9a-fA-F]{6,}\b", re.IGNORECASE),
    # Standalone short git SHAs — require explicit context word
    # (``commit``, ``sha``, ``hash``) before the hex to avoid matching
    # everyday hex-looking words (``decade``, ``acceded``, ``café``
    # is filtered out by the \b anchors + the context word).
    re.compile(
        r"\b(?:commit|sha|hash|rev|ref)\s+[0-9a-f]{7,40}\b",
        re.IGNORECASE,
    ),
    # Full 40-char git SHAs — long enough that they can stand alone
    # without context (no English word is 40 hex chars).
    re.compile(r"\b[0-9a-f]{40}\b", re.IGNORECASE),
)


def _content_is_identifier_bearing(content: str) -> bool:
    """Return True iff ``content`` carries any identifier-shaped token.

    See module docstring for the rationale. Used by
    ``CheckSemanticDuplicate`` to short-circuit semantic dedup when
    the identifier IS the disambiguator and the surrounding template
    is what the embedder sees.

    Returns False (don't pre-filter) for any non-string or empty input.
    """
    if not isinstance(content, str) or not content:
        return False
    return any(pat.search(content) for pat in _IDENTIFIER_PATTERNS)
