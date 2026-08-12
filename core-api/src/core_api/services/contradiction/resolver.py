"""ConflictResolver — run L1+L2+L3 on a candidate pair and persist the record (A55).

``record_conflict(new_memory, candidate)`` classifies the pair (L1 relationship +
L2 diagnosis), resolves an action with the safety invariants (L3), and writes a
``memory_conflicts`` row via storage. It returns the stored record (or None on a
storage failure — best-effort, like the rest of the post-commit contradiction work).

ADDITIVE by design: this writes the classification RECORD only. The memory-row
effect (``status`` / ``supersedes_id``) is still applied by the detector and is
unchanged, so retrieval behaviour is identical (the golden differential test is
the gate). Wiring this onto the engine-ON path is done separately behind a flag.
"""

from __future__ import annotations

import logging

from core_api.clients.storage_client import get_storage_client
from core_api.services.contradiction.diagnosis import classify
from core_api.services.contradiction.resolution import resolve

logger = logging.getLogger(__name__)

_CREATED_BY = "contradiction-engine"


def _evidence_strength(relationship: str, is_inferred: bool) -> str:
    """Map to the memory_conflicts.evidence_strength vocabulary
    (explicit / entailed / probabilistic)."""
    if relationship == "probabilistic":
        return "probabilistic"
    if is_inferred:
        return "entailed"
    return "explicit"


async def record_conflict(
    new_memory: dict,
    candidate: dict,
    *,
    tenant_id: str,
    fleet_id: str | None,
    tenant_config=None,
) -> dict | None:
    """Classify + resolve a candidate pair and persist the ``memory_conflicts``
    record. Best-effort: returns None (logged) if the write fails."""
    result = await classify(new_memory, candidate, tenant_config=tenant_config)

    # Invariant input: weak (inferred) evidence must not overturn explicit facts.
    is_inferred = bool(new_memory.get("is_inferred") or candidate.get("is_inferred"))
    gate_confidence = min(result.relationship_confidence, result.diagnosis_confidence)
    resolution = resolve(
        result.relationship,
        result.diagnosis,
        is_inferred=is_inferred,
        confidence=gate_confidence,
    )

    payload = {
        "tenant_id": tenant_id,
        "fleet_id": fleet_id,
        "new_memory_id": str(new_memory["id"]),
        "old_memory_id": str(candidate["id"]),
        "relationship": result.relationship,
        "relationship_confidence": result.relationship_confidence,
        "diagnosis": result.diagnosis,
        "diagnosis_confidence": result.diagnosis_confidence,
        "evidence_strength": _evidence_strength(result.relationship, is_inferred),
        "action": resolution.action,
        "audit_reason": resolution.audit_reason,
        "created_by": _CREATED_BY,
    }

    try:
        sc = get_storage_client()
        return await sc.record_memory_conflict(payload)
    except Exception:
        logger.warning(
            "memory_conflicts write failed for new=%s old=%s",
            new_memory.get("id"),
            candidate.get("id"),
            exc_info=True,
        )
        return None
