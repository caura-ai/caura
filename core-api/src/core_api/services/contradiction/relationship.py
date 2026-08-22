"""L1 — relationship classification (A55, unified contradiction model).

``classify_relationship(new_memory, candidate)`` returns ``(relationship,
confidence)`` — the semantic relationship between a candidate pair, independent
of whether they contradict. It populates ``memory_conflicts.relationship``.

Two branches (see benchmark/a55/subtasks/12-L1-relationship-logic.md):
  * **deterministic** — RDF exact-value (same subject_entity_id + predicate,
    different object_value) short-circuits to ``exact_value`` with no LLM call.
  * **LLM** — the detector's ``CONTRADICTION_PROMPT`` EXTENDED with a relationship
    instruction; ``extract_relationship`` reads the field, with a derivation
    fallback from the model's existing ``non_conflict_reason`` taxonomy.

No-degradation: the legacy ``CONTRADICTION_PROMPT`` and the verdict path are
untouched — L1 appends its instruction to a copy. Nothing wires
``classify_relationship`` into production yet (the engine resolver does so on the
ON path in a later slice), so this module is dormant and behaviour-neutral.
"""

from __future__ import annotations

import logging

from core_api.config import settings
from core_api.providers._retry import call_with_fallback
from core_api.services.contradiction_detector import CONTRADICTION_PROMPT

logger = logging.getLogger(__name__)

# Mirror of the model's L1 vocabulary (common/models/memory_conflict.RELATIONSHIPS).
# Imported lazily-safe as a literal so this module never depends on the ORM layer.
RELATIONSHIPS: frozenset[str] = frozenset(
    {
        "exact_value",
        "negation",
        "entailed",
        "constraint",
        "probabilistic",
        "scope_apparent",
        "refinement",
    }
)

# Confidence by how the label was obtained.
_CONF_RDF = 0.95
_CONF_MODEL = 0.80
_CONF_DERIVED = 0.55
_CONF_FALLBACK = 0.50

# Derive a relationship from the model's existing non_conflict_reason taxonomy
# when it doesn't emit an explicit ``relationship`` (older/partial responses).
_NCR_TO_RELATIONSHIP: dict[str, str] = {
    "refinement": "refinement",
    "scope_mismatch": "scope_apparent",
    "list_valued_predicate": "constraint",
    "temporal_supersession": "exact_value",  # temporal aspect is L2, slot is exact_value
    "event_restatement": "entailed",
    "conditional_unrealized": "probabilistic",
}

# Appended to a COPY of CONTRADICTION_PROMPT — never mutates the original.
_RELATIONSHIP_INSTRUCTION = """

In the SAME JSON object, ALSO include a "relationship" field: the semantic
relationship between Statement A and Statement B, independent of whether they
contradict. Choose EXACTLY one:
  - "exact_value": same attribute/slot of the same subject, different single
    value (X lives in Tel Aviv vs Haifa; flight at 6pm vs 8pm).
  - "negation": one asserts P, the other asserts not-P (is active vs is inactive).
  - "entailed": one logically implies or restates the other (joined vs was hired;
    "in Munich" implies "in Germany").
  - "constraint": a multi-valued / list predicate where both can hold at once
    (speaks English; speaks French).
  - "probabilistic": one or both are hedged, hypothetical, or uncertain
    ("might move to B", "probably prefers X").
  - "scope_apparent": same attribute under different implicit qualifiers — time
    window, whole vs part, or context (weekday vs weekend); both may hold.
  - "refinement": one is a more specific version of the other (Europe vs Munich;
    Q3 vs September 15).
Return ONE JSON object containing all the fields above PLUS "relationship".
"""


def _is_rdf_exact_value(new_memory: dict, candidate: dict) -> bool:
    """Deterministic RDF exact-value: same subject_entity_id + predicate, with
    two different non-null object_values. No LLM needed."""
    ns, cs = new_memory.get("subject_entity_id"), candidate.get("subject_entity_id")
    np_, cp = new_memory.get("predicate"), candidate.get("predicate")
    nv, cv = new_memory.get("object_value"), candidate.get("object_value")
    return bool(
        ns
        and cs
        and str(ns) == str(cs)
        and np_
        and cp
        and np_ == cp
        and nv is not None
        and cv is not None
        and nv != cv
    )


def extract_relationship(raw: dict) -> tuple[str | None, str]:
    """Read the ``relationship`` from a model response, or derive it.

    Returns ``(relationship, source)`` where source is ``"model"`` (explicit),
    ``"derived"`` (mapped from non_conflict_reason / contradicts), or ``"none"``.
    """
    if not isinstance(raw, dict):
        return None, "none"

    val = raw.get("relationship")
    if isinstance(val, str) and val in RELATIONSHIPS:
        return val, "model"

    ncr = raw.get("non_conflict_reason")
    if isinstance(ncr, str) and ncr in _NCR_TO_RELATIONSHIP:
        return _NCR_TO_RELATIONSHIP[ncr], "derived"

    if raw.get("contradicts") is True:
        # Same-subject, mutually-exclusive, no non-conflict shape -> same slot.
        return "exact_value", "derived"

    return None, "none"


async def _llm_raw(prompt: str, tenant_config=None) -> dict:
    """LLM call seam (patched in tests). Uses the standard provider-fallback
    chain; returns the parsed JSON dict (``{}`` on total failure)."""
    provider_name = (
        tenant_config.entity_extraction_provider if tenant_config else settings.entity_extraction_provider
    )

    async def _do(llm) -> dict:
        return await llm.complete_json(prompt)

    return await call_with_fallback(
        primary_provider_name=provider_name,
        call_fn=_do,
        fake_fn=lambda: {},
        tenant_config=tenant_config,
        service_label="relationship",
        model_attr="entity_extraction_model",
        timeout=10.0,
    )


async def classify_relationship(
    new_memory: dict,
    candidate: dict,
    *,
    tenant_config=None,
) -> tuple[str, float]:
    """L1: classify the relationship for a candidate pair. Always returns a
    valid relationship (defaults to ``exact_value`` when unclassifiable, since a
    pair only reaches L1 after retrieval flagged it as a conflict candidate).

    The entity-aware variant (Path C, using ENTITY_AWARE_CONTRADICTION_PROMPT +
    resolved entity context) is a follow-up; this cut covers the base path.
    """
    # Branch 1 — deterministic RDF exact-value.
    if _is_rdf_exact_value(new_memory, candidate):
        return "exact_value", _CONF_RDF

    # Branch 2 — LLM. Extend a COPY of the base prompt (never mutate the original).
    new_content = str(new_memory.get("content", ""))[:500]
    old_content = str(candidate.get("content", ""))[:500]
    prompt = (
        CONTRADICTION_PROMPT.format(new_content=new_content, old_content=old_content)
        + _RELATIONSHIP_INSTRUCTION
    )

    raw = await _llm_raw(prompt, tenant_config)
    relationship, source = extract_relationship(raw)

    if relationship is None:
        logger.info(
            "L1 relationship unclassified for new=%s cand=%s -> defaulting exact_value",
            new_memory.get("id"),
            candidate.get("id"),
        )
        return "exact_value", _CONF_FALLBACK

    conf = _CONF_MODEL if source == "model" else _CONF_DERIVED
    return relationship, conf
