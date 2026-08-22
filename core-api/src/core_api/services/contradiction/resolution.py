"""L3 — resolution: map (relationship x diagnosis x evidence) -> action (A55).

Pure decision function + the unified model's safety invariants. Given the L1
relationship, the L2 diagnosis, and the evidence dimensions (is_inferred /
relationship strength / confidence), pick a resolution *action* and a
human-readable *audit_reason*.

IMPORTANT — this only RECOMMENDS an action for the ``memory_conflicts`` record.
Applying an effect to the memory row (``status`` / ``supersedes_id``) is done by
the detector today and is unchanged; the write step (next) persists this
recommendation additively without mutating that effect, so behaviour is preserved
(the golden differential test is the gate). Turning a recommendation like
``replace`` into an actual overwrite is a later, deliberately-flagged step.
"""

from __future__ import annotations

from dataclasses import dataclass

# Mirror of common/models/memory_conflict.ACTIONS.
ACTIONS: frozenset[str] = frozenset(
    {
        "replace",
        "supersede",
        "scope",
        "merge",
        "split_entity",
        "downweight",
        "mark_disputed",
        "ask",
        "no_op",
    }
)

# Diagnosis is the dominant signal for the action.
_DIAGNOSIS_ACTION: dict[str, str] = {
    "temporal_change": "supersede",  # state changed; keep old as history
    "correction": "replace",  # old was wrong; new replaces it
    "scope_difference": "scope",  # both hold under different qualifiers
    "entity_mismatch": "split_entity",  # different entities; don't supersede
    "write_error": "mark_disputed",  # malformed; flag for review
    "unresolved": "ask",  # can't decide from text
}

# Relationships where both statements hold — never supersede/replace.
_BOTH_HOLD: frozenset[str] = frozenset({"entailed", "constraint", "refinement"})

# Actions that overturn an existing fact — gated by the safety invariants.
_DESTRUCTIVE: frozenset[str] = frozenset({"replace", "supersede"})

# Below this classification confidence, don't overturn — flag instead.
_LOW_CONFIDENCE = 0.5


@dataclass(frozen=True)
class Resolution:
    action: str
    audit_reason: str


def resolve(
    relationship: str,
    diagnosis: str,
    *,
    is_inferred: bool = False,
    confidence: float | None = None,
) -> Resolution:
    """Map the classification to a resolution action, applying safety invariants.

    Always returns a valid ``ACTIONS`` value. The invariants (in order) protect
    against weak evidence overturning strong facts — the unified model's rule
    that *inference is weaker than explicit contradiction*.
    """
    # Both-hold relationships resolve to nothing regardless of diagnosis.
    if relationship in _BOTH_HOLD:
        return Resolution("no_op", f"{relationship}: both statements hold; no resolution needed")

    action = _DIAGNOSIS_ACTION.get(diagnosis, "ask")
    reason = f"{relationship}/{diagnosis} -> {action}"

    # Invariant: a system-inferred memory must not overturn an explicit fact.
    if is_inferred and action in _DESTRUCTIVE:
        return Resolution(
            "mark_disputed",
            reason + " | downgraded: inferred evidence must not overturn an explicit fact",
        )

    # Invariant: a hedged/uncertain relationship softens, never hard-replaces.
    if relationship == "probabilistic" and action in _DESTRUCTIVE:
        return Resolution(
            "downweight",
            reason + " | probabilistic relationship: downweight, not overturn",
        )

    # Invariant: low classification confidence flags rather than overturns.
    if confidence is not None and confidence < _LOW_CONFIDENCE and action in _DESTRUCTIVE:
        return Resolution(
            "mark_disputed",
            reason + f" | low confidence {confidence:.2f}: flag rather than overturn",
        )

    return Resolution(action, reason)
