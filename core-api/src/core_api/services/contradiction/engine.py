"""ContradictionEngine — single seam over contradiction detection (A55).

**Phase 1 is a behaviour-preserving facade.** ``evaluate_async`` dispatches by
``trigger`` to the *existing* detector entry points; no detection logic is
rewritten here. The 887 contradiction tests and the golden differential test
(``tests/test_contradiction_engine_golden.py``) guard byte-parity.

**Phase 2 (A55 / sub-task 1d)** will move the resolution write into a
``ConflictResolver`` here and, behind its own flag, additionally persist the
``memory_conflicts`` record + ``confidence``/``is_inferred``/``scope``. That is
additive — never a change to the legacy effect.

The flag that selects this arch over the legacy call sites lives in
``dispatch.py`` (``run_contradiction_detection``), not here — the engine is the
*new* arch, not the switch.
"""

from __future__ import annotations

from enum import Enum
from uuid import UUID


class Trigger(str, Enum):
    """Why contradiction detection is running — determines Path A vs Path C.

    Path A (RDF -> Semantic) fires on the write / embedding lifecycle. Path C
    (retract -> forward, entity-aware) fires after entity extraction. Keeping
    the reason explicit lets the single ``evaluate_async`` entry preserve the
    exact per-trigger routing the scattered call sites have today.
    """

    WRITE = "write"  # post-commit, embedding present (Path A)
    EMBED = "embed"  # ENRICHED / EMBEDDED back-channel (Path A)
    UPDATE = "update"  # content edit re-fire (Path A)
    BULK = "bulk"  # bulk write, per item (Path A)
    REEMBED = "reembed"  # re-embed re-fire (Path A)
    ENTITY = "entity"  # post entity-extraction (Path C)


_PATH_A: frozenset[Trigger] = frozenset(
    {Trigger.WRITE, Trigger.EMBED, Trigger.UPDATE, Trigger.BULK, Trigger.REEMBED}
)


class ContradictionEngine:
    """Unifies Path A and Path C behind one entry point.

    Stateless — instantiate per call (or reuse); it holds no connection. The
    storage client + Redis locks are acquired inside the delegated detector
    functions exactly as today, so idempotency keys and CAS anchors are
    unchanged.
    """

    async def evaluate_async(
        self,
        memory_id: UUID,
        tenant_id: str,
        fleet_id: str | None,
        *,
        trigger: Trigger,
        content: str | None = None,
        embedding: list[float] | None = None,
        new_memory: dict | None = None,
    ) -> None:
        # Lazy import: the detector module imports heavy provider/config code;
        # importing at call time keeps this package cheap to import and avoids
        # an import cycle (detector -> services -> contradiction package).
        from core_api.services import contradiction_detector as legacy

        if trigger is Trigger.ENTITY:
            await legacy.detect_contradictions_by_entities_async(memory_id, tenant_id, fleet_id)
            return

        if trigger in _PATH_A:
            # Path A needs content + embedding. A caller without the embedding
            # (deferred-embedding path) is a no-op here — detection re-fires
            # from the EMBEDDED back-channel once the vector lands, exactly as
            # the legacy call sites behave.
            if content is None or embedding is None:
                return
            await legacy.detect_contradictions_async(
                memory_id,
                tenant_id,
                fleet_id,
                content,
                embedding,
                new_memory=new_memory,
            )
            return

        raise ValueError(f"unknown contradiction trigger: {trigger!r}")
