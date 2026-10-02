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


# A42 — parenthetical qualifiers, e.g. the "(delaware)" in "acme (delaware)".
# Matches the discriminator vocabulary ``_reattach_subject_discriminators``
# already established ("#NNNN" and parentheticals); deliberately NOT any
# trailing word, because unbracketed tokens are usually harmless surface
# variation ("acme" / "acme corp") rather than a distinguisher.
QUALIFIER_RE = re.compile(r"[(\[]([^)\]]{1,64})[)\]]")


def qualifier_signature(name: str) -> frozenset[str]:
    """Bracketed qualifiers in ``name``, normalised for comparison."""
    return frozenset(
        " ".join(m.strip().lower().split())
        for m in QUALIFIER_RE.findall(name)
        if m.strip()
    )


def same_identifier_signature(a: str, b: str) -> bool:
    """Two names may only merge if nothing in them says they are different things.

    CAURA graph-build fix (B): same set of digit-bearing identifier tokens.
    Synthetic suffix-distinct names like 'comet #0002' vs 'comet #0012' embed
    near-identically and trip the 0.85 similarity merge, collapsing distinct
    entities into one contaminated mega-node.

    A42 (A33 mechanism ②): digits alone are too narrow. Two genuinely distinct
    entities distinguished by a NON-digit qualifier — 'acme (delaware)' vs
    'acme (ohio)' — both yield an empty digit set, compare equal, and merge.
    Downstream that reads as same_subject=true and produces a false
    contradiction between two different things.

    Asymmetry is deliberate: a name with NO qualifier merges freely with a
    qualified one ('acme' vs 'acme (ohio)' -> allowed). An absent qualifier
    means "unspecified", not "different", and blocking it would strand every
    qualified mention from its own plain surface form. The chosen failure
    direction favours coalescence — an over-merge is visible and recoverable,
    whereas an entity that never coalesces fragments the graph silently.
    """
    ta = set(re.findall(r"\d[\w.\-]*", a.lower()))
    tb = set(re.findall(r"\d[\w.\-]*", b.lower()))
    if ta != tb:
        return False
    qa, qb = qualifier_signature(a), qualifier_signature(b)
    # Only a CONFLICT between two present qualifiers blocks the merge.
    return not qa or not qb or qa == qb


def has_identifier_or_qualifier(name: str) -> bool:
    """Whether ``name`` carries something ``same_identifier_signature`` compares:
    a digit-bearing token or a bracketed qualifier."""
    return bool(re.search(r"\d", name)) or bool(qualifier_signature(name))
