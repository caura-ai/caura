"""Conservative entity-name normalisation for canonical resolution (WT-2).

Wet-test defect WT-2: extraction canonicalised ONE real-world subject as TWO
entity rows — ``new analytics service`` (memory: "…PostgreSQL 16 … for the
new analytics service") and ``analytics service`` (memory: "We migrated the
analytics service …"). A split subject splits the knowledge graph and makes
entity-scoped contradiction detection structurally blind (WT-3).

The rule here is deliberately SMALL and deterministic — no fuzzy matching,
no embeddings, no substring heuristics; those merge entities that must stay
apart. Two names refer to the same entity iff their *canonical match keys*
are equal, where the key is computed as:

1. normalise: lowercase, strip, collapse internal whitespace;
2. iteratively strip ONE leading token at a time from a fixed set of
   determiners / temporal qualifiers (``the a an new old current existing
   legacy``), but ONLY while the remainder still has at least TWO tokens.

The two-token guard is the safety rule for names where the "qualifier" is
part of the name itself: ``new york`` must NOT collapse to ``york`` (nor
``the office`` to ``office``). With the guard, ``canonical_match_key("new
york") == "new york"`` — a one-token remainder is evidence the leading word
is load-bearing, so it is kept. Multi-token remainders (``new analytics
service`` → ``analytics service``) keep enough specificity that the leading
qualifier is overwhelmingly descriptive, not nominal. The guard applies per
strip step, so stacked qualifiers still reduce safely: ``the new analytics
service`` → ``analytics service``, while ``the new york`` stops at
``new york``.

Symmetric by construction: an incoming ``analytics service`` matches an
existing ``new analytics service`` and vice versa, because both map to the
same key. Comparison is only ever key-to-key.

Shared by core-api (extraction-batch dedupe in
``entity_extraction_worker``) and core-storage-api (Phase 1.5 normalised
match in ``entity_bulk_resolve``) so both layers agree on what "the same
name" means.
"""

from __future__ import annotations

import re

# Leading tokens that are (almost always) descriptive rather than nominal.
# FIXED, small, and not configurable on purpose — every addition widens the
# merge surface. See module docstring for the two-token guard that protects
# the cases where one of these IS part of the name.
ENTITY_NAME_QUALIFIERS: frozenset[str] = frozenset(
    {"the", "a", "an", "new", "old", "current", "existing", "legacy"}
)

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_entity_name(name: str) -> str:
    """Lowercase, strip, and collapse internal whitespace. Nothing else."""
    return _WHITESPACE_RE.sub(" ", name.strip().lower())


def canonical_match_key(name: str) -> str:
    """Return the canonical comparison key for an entity surface form.

    Two surface forms denote the same entity (for resolution purposes) iff
    their keys are equal. See module docstring for the exact rule and its
    safety guard.
    """
    tokens = normalize_entity_name(name).split(" ")
    # Strip one leading qualifier per step, only while the remainder keeps
    # >= 2 tokens ("new york" guard — a one-token remainder means the
    # leading word is likely part of the name, so stop).
    while len(tokens) >= 3 and tokens[0] in ENTITY_NAME_QUALIFIERS:
        tokens = tokens[1:]
    return " ".join(tokens)
