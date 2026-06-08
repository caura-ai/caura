"""Cluster fingerprint — canonical identity for skill candidates (SF-103).

The Phase-1 critical-path piece. Without canonical cluster identity,
every Forge re-run mints near-duplicate candidates and the Skills
Inbox floods with noise. Plan §8.

Format: ``fp:v1:<sha256_hex>``. The ``v1`` prefix is the formula
version — any change to the canonicalization rules below MUST bump
to v2 (and run a back-fill against ``forge_rejected_fingerprints``
so the cooloff memory stays meaningful across versions).

Canonical form (sha256 input):

    goal=<canonical-goal-tokens-sorted-deduped>
    domain=<lowercase-stripped>
    entities=<top-5-entity-ids-sorted-asc>
    steps=<step-skeletons-pipe-joined-order-preserved>

Stability properties (pinned by property-based unit tests below):

  P1. Determinism: same inputs ⇒ same fingerprint.
  P2. Permutation invariance: shuffling entity_ids, goal_phrase
      tokens, or letter casing in domain does NOT change the
      fingerprint.
  P3. Step ordering: step skeleton ORDER is significant (a procedure
      ABA differs from AAB), but the WORDS WITHIN a step are order-
      preserved and stopword-stripped.
  P4. Top-K stability: adding a 6th low-centrality entity does NOT
      change the fingerprint (top-5 cap absorbs the perturbation).
  P5. Token normalization: simple plural stripping and stopword
      removal absorb small surface perturbations ("deploys" vs
      "deploy", "the deploy" vs "deploy").
  P6. Domain canonicalization: " DevOps " ≡ "devops".

The empirical ≥99% stability target (plan §8) is enforced by the
eval harness in SF-105 — this module ships the formula + the
invariant tests; the eval harness ships the live measurement.

This module does NOT call any LLM. ``goal_phrase`` is passed in
(the caller — usually the cluster step in SF-104 — invokes the
LLM and threads the result here). Keeps the fingerprint trivially
unit-testable and removes a network dependency from the hot path.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# Formula version. Bumping this is a coordination event: all live
# rejected-fingerprint cooloffs become orphaned references. Plan §8
# DRIFT HANDLING describes the migration.
FINGERPRINT_FORMULA_VERSION: str = "v1"

# Top-K caps. The smaller these are, the more stable the
# fingerprint under cluster growth (a new trace is unlikely to crack
# into top 5 entities by centrality).
ENTITY_TOP_K: int = 5
GOAL_PHRASE_TOP_K: int = 6
STEP_WORDS_CAP: int = 4

# Stopwords stripped before tokenization. Curated minimal set —
# only the ones that genuinely add noise without distinguishing
# procedures. Words like "deploy" or "step" are NOT here even
# though they're common, because their presence/absence DOES
# distinguish procedures.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "in",
        "on",
        "at",
        "by",
        "for",
        "to",
        "of",
        "with",
        "into",
        "this",
        "that",
        "these",
        "those",
        "and",
        "or",
        "but",
        "it",
        "its",
        "do",
        "did",
        "does",
        "as",
    }
)


# ── Data shape ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClusterFingerprintInputs:
    """The four canonical-form ingredients (plan §8).

    All are required; emptiness is allowed but contributes to the
    hash. An all-empty cluster still has a well-defined (degenerate)
    fingerprint — useful as a sentinel for "no canonical form yet"
    rather than a None.

    ``entity_centralities`` is an optional dict mapping ``entity_id``
    to a centrality score in [0, 1] (e.g. PageRank within the
    cluster's entity subgraph). When present, top-K is by centrality
    DESC then tie-broken by id ASC; when absent, top-K is by id ASC.
    """

    goal_phrase: str
    domain: str
    entity_ids: list[str] = field(default_factory=list)
    step_skeleton: list[str] = field(default_factory=list)
    entity_centralities: dict[str, float] | None = None


@dataclass(frozen=True)
class Fingerprint:
    """Pair of the rendered fp string and the canonical_form it was
    hashed from. The latter is logged when diagnosing fingerprint
    drift (e.g. "why did fp shift between two Forge runs?") so the
    operator can diff the canonical forms directly.
    """

    fp: str
    canonical_form: str


# ── Public API ────────────────────────────────────────────────────


def compute_fingerprint(inputs: ClusterFingerprintInputs) -> Fingerprint:
    """Render and hash. Pure function. Deterministic. No I/O."""
    canonical = render_canonical_form(inputs)
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Fingerprint(
        fp=f"fp:{FINGERPRINT_FORMULA_VERSION}:{h}",
        canonical_form=canonical,
    )


def render_canonical_form(inputs: ClusterFingerprintInputs) -> str:
    """The sha256 input. Exposed publicly so eval/debug tooling can
    diff two clusters' canonical forms to explain why they hashed
    differently."""
    goal = canonical_goal_phrase(inputs.goal_phrase)
    domain = canonical_domain(inputs.domain)
    entities = canonical_entities(
        inputs.entity_ids,
        centralities=inputs.entity_centralities,
    )
    steps = canonical_step_skeleton(inputs.step_skeleton)
    return f"goal={goal}\ndomain={domain}\nentities={','.join(entities)}\nsteps={'|'.join(steps)}"


# ── Canonicalization primitives ───────────────────────────────────


def canonical_goal_phrase(text: str) -> str:
    """Tokenize → lowercase → strip punctuation → drop stopwords →
    strip simple plurals → DEDUPE → SORT → take top
    :data:`GOAL_PHRASE_TOP_K`.

    The sort is what makes "deploy hangs eu-west" ≡ "eu-west deploy
    hangs" — the bag-of-words distillation is what we want for
    clustering identity.
    """
    if not text:
        return ""
    tokens = _tokenize(text)
    tokens = [t for t in tokens if t not in _STOPWORDS]
    tokens = [_strip_simple_plural(t) for t in tokens]
    tokens = sorted(set(tokens))
    return " ".join(tokens[:GOAL_PHRASE_TOP_K])


def canonical_domain(domain: str) -> str:
    """Lower + strip outer whitespace. Domain values come from a
    curated enum (eToro: 13 values like ``devops``, ``security``,
    ``marketing`` — see survey memo) so the canonicalization is
    minimal."""
    return (domain or "").strip().lower()


def canonical_entities(
    entity_ids: list[str],
    *,
    centralities: dict[str, float] | None = None,
) -> list[str]:
    """Top-K entities by centrality (when available), else by id
    sort. Output is always sorted ASCENDING (canonical form
    requirement: permuting the input must not change the output).

    Ties on centrality are broken by id ASC (deterministic).
    """
    if not entity_ids:
        return []
    if centralities:
        # Dedupe BEFORE ranking — without this the centrality path
        # would diverge from the no-centrality path (which dedupes
        # via ``sorted(set(...))``). Duplicate inputs would otherwise
        # burn top-K slots and silently shift the canonical form.
        # Sort key is (centrality DESC, id ASC) — id ASC breaks
        # centrality ties deterministically.
        ranked = sorted(
            set(entity_ids),
            key=lambda eid: (-centralities.get(eid, 0.0), eid),
        )
        top = ranked[:ENTITY_TOP_K]
    else:
        # No centrality info — fall back to id ASC then take first K.
        # Deterministic but not centrality-aware; OK as a degenerate
        # case when the entity-resolution layer hasn't scored yet.
        top = sorted(set(entity_ids))[:ENTITY_TOP_K]
    return sorted(top)


def canonical_step_skeleton(steps: list[str]) -> list[str]:
    """Per-step normalization. Step ORDER is preserved (procedure
    semantics depend on it). Within each step:

      - lowercase
      - strip punctuation
      - drop stopwords
      - strip simple plurals
      - cap to :data:`STEP_WORDS_CAP` words
      - preserve word order within the step (the verb-object
        ordering is semantically meaningful, unlike the bag-of-words
        goal phrase)

    Empty steps after normalization are kept as the empty string —
    losing them would shift downstream step indices and break
    cluster identity ("step 3 was a no-op" is meaningful).
    """
    out: list[str] = []
    for raw in steps:
        words = _tokenize(raw)
        words = [w for w in words if w not in _STOPWORDS]
        words = [_strip_simple_plural(w) for w in words]
        words = words[:STEP_WORDS_CAP]
        out.append(" ".join(words))
    return out


# ── Internals ─────────────────────────────────────────────────────


_NON_ALPHANUM = re.compile(r"[^a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    """Lowercase, collapse any non-alphanumeric run to a single
    space, then split. Cheap, deterministic, no dependencies."""
    if not text:
        return []
    return [t for t in _NON_ALPHANUM.split(text.lower()) if t]


def _strip_simple_plural(token: str) -> str:
    """Very rough English-plural stripping. We do this so "deploys"
    and "deploy" hash identically. It's a known under-approximation
    of real lemmatization — picks up regular -s plurals but not
    irregulars (criteria/criterion, indices/index).

    Protections:
      - Words ≤ 4 chars are left alone ("ass", "ous" wouldn't
        survive otherwise).
      - Double-s endings ("press", "miss", "class") are protected.
      - "-us" endings ("us", "thus", "bus") are protected.
    """
    if len(token) <= 4:
        return token
    if token.endswith("ss") or token.endswith("us"):
        return token
    if token.endswith("ies"):
        # "tries" → "try", "babies" → "baby" (regular English -ies plural).
        # Over-approximation OK — stability > accuracy at this layer.
        # No length guard needed: the ``if len(token) <= 4`` early-return
        # at the top of the function already excluded tokens of 4 chars
        # or fewer, so any token reaching here is ≥5 chars (e.g. ``flies``).
        return token[:-3] + "y"
    if token.endswith("s"):
        return token[:-1]
    return token
