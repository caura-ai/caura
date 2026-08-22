"""L2 — diagnosis (cause) classification + the combined L1+L2 classifier (A55).

L2 answers *why* a candidate pair differs: correction / temporal_change /
scope_difference / entity_mismatch / write_error / unresolved. It builds on L1
(``relationship``) and the model's existing ``non_conflict_reason`` / same_subject
signals, so most diagnoses are derivable; the model can also emit ``diagnosis``
explicitly via the extended prompt.

``classify()`` is the combined entry the engine resolver will call — ONE LLM call
yields both the L1 relationship and the L2 diagnosis. Like L1 this is dormant
(nothing wires it into production yet) and never mutates the legacy prompt/verdict.
See benchmark/a55/subtasks/12-L1-relationship-logic.md (L2 section in 13-…).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core_api.services.contradiction.relationship import (
    _CONF_DERIVED,
    _CONF_FALLBACK,
    _CONF_MODEL,
    _CONF_RDF,
    _RELATIONSHIP_INSTRUCTION,
    CONTRADICTION_PROMPT,
    _is_rdf_exact_value,
    _llm_raw,
    extract_relationship,
)

logger = logging.getLogger(__name__)

# Mirror of common/models/memory_conflict.DIAGNOSES.
DIAGNOSES: frozenset[str] = frozenset(
    {
        "correction",
        "temporal_change",
        "scope_difference",
        "entity_mismatch",
        "write_error",
        "unresolved",
    }
)

# RDF exact-value is ambiguous between "moved" (temporal_change) and "was wrong"
# (correction). Default to temporal_change so the eventual L3 action (supersede)
# matches today's RDF behaviour; a temporal/LLM signal can refine it later.
_CONF_RDF_DEFAULT_DIAG = 0.50

_DIAGNOSIS_INSTRUCTION = """

In the SAME JSON object, ALSO include a "diagnosis" field: WHY the two statements
differ. Choose EXACTLY one:
  - "correction": the newer statement fixes an error in the older; the old value
    was simply wrong ("actually it's X", "correction: Y").
  - "temporal_change": the subject's state genuinely changed over time; both were
    true in sequence (moved cities, was promoted, plan -> shipped).
  - "scope_difference": they describe the same subject under different implicit
    qualifiers (time window, whole vs part, context) and both can hold.
  - "entity_mismatch": they are actually about different real-world entities
    (two people sharing a name, two builds, two events).
  - "write_error": one is a malformed / mistaken write (garbled, wrong field).
  - "unresolved": the cause cannot be determined from the text.
Return ONE JSON object with "relationship" AND "diagnosis" AND the other fields.
"""


def extract_diagnosis(raw: dict, relationship: str | None) -> tuple[str | None, str]:
    """Read ``diagnosis`` from a model response, or derive it from L1 +
    non_conflict_reason + same_subject. Returns ``(diagnosis, source)``."""
    if not isinstance(raw, dict):
        return None, "none"

    val = raw.get("diagnosis")
    if isinstance(val, str) and val in DIAGNOSES:
        return val, "model"

    ncr = raw.get("non_conflict_reason")
    if raw.get("same_subject") is False or ncr == "same_name_distinct_subject":
        return "entity_mismatch", "derived"
    if ncr == "temporal_supersession":
        return "temporal_change", "derived"
    if ncr == "scope_mismatch" or relationship == "scope_apparent":
        return "scope_difference", "derived"
    if raw.get("contradicts") is True and relationship in {"exact_value", "negation"}:
        return "correction", "derived"
    if relationship in {"entailed", "constraint", "refinement", "probabilistic"}:
        # Both-hold shapes — nothing to resolve.
        return "unresolved", "derived"
    return None, "none"


def _conf_for(source: str) -> float:
    return {
        "model": _CONF_MODEL,
        "derived": _CONF_DERIVED,
    }.get(source, _CONF_FALLBACK)


@dataclass(frozen=True)
class ClassifyResult:
    """Combined L1 + L2 result for a candidate pair."""

    relationship: str
    relationship_confidence: float
    diagnosis: str
    diagnosis_confidence: float


async def classify(
    new_memory: dict,
    candidate: dict,
    *,
    tenant_config=None,
) -> ClassifyResult:
    """Combined L1 (relationship) + L2 (diagnosis) classification — ONE LLM call.

    Always returns valid values (relationship defaults to ``exact_value``,
    diagnosis to ``unresolved``), since a pair only reaches here after retrieval
    flagged it as a conflict candidate.
    """
    # Deterministic RDF exact-value: L1 is certain, L2 defaults (see above).
    if _is_rdf_exact_value(new_memory, candidate):
        return ClassifyResult("exact_value", _CONF_RDF, "temporal_change", _CONF_RDF_DEFAULT_DIAG)

    new_content = str(new_memory.get("content", ""))[:500]
    old_content = str(candidate.get("content", ""))[:500]
    prompt = (
        CONTRADICTION_PROMPT.format(new_content=new_content, old_content=old_content)
        + _RELATIONSHIP_INSTRUCTION
        + _DIAGNOSIS_INSTRUCTION
    )
    raw = await _llm_raw(prompt, tenant_config)

    rel_label, rel_src = extract_relationship(raw)
    if rel_label is None:
        rel_label, rel_src = "exact_value", "fallback"
    diag_label, diag_src = extract_diagnosis(raw, rel_label)
    if diag_label is None:
        diag_label, diag_src = "unresolved", "fallback"
        logger.info(
            "L2 diagnosis unclassified for new=%s cand=%s -> unresolved",
            new_memory.get("id"),
            candidate.get("id"),
        )

    return ClassifyResult(rel_label, _conf_for(rel_src), diag_label, _conf_for(diag_src))
