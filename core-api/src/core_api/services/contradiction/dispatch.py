"""Flag-gated router between the legacy detector and the ContradictionEngine.

``settings.contradiction_engine_enabled`` (default ``False``) selects the arch:

  * ``False`` -> invoke the legacy detector entry — byte-identical to the
    pre-engine trigger sites.
  * ``True``  -> route through ``ContradictionEngine.evaluate_async``.

**Sync on purpose.** ``run_contradiction_detection`` is a *regular* function that
returns an awaitable. On the legacy path it calls the detector entry
*synchronously* and returns its coroutine, so a call site that schedules the
result — ``track_task(run_contradiction_detection(...))`` — keeps the exact
schedule-time semantics the direct ``detect_contradictions_async(...)`` call had
(the detector coroutine is created at schedule time, run when awaited). Awaiting
the returned object runs detection in either arch.

Trigger sites call ``run_contradiction_detection(...)`` instead of the legacy
functions directly, so the whole contradiction surface flips with one flag and
the legacy arch can be retired once the engine is proven. In Phase 1 both
branches reach the same detector code — the routing-parity test
(``tests/test_contradiction_engine_routing.py``) pins that, and the golden
differential test pins the detection outcomes.
"""

from __future__ import annotations

from collections.abc import Awaitable
from uuid import UUID

from core_api.config import settings
from core_api.services.contradiction.engine import _PATH_A, ContradictionEngine, Trigger


async def _noop() -> None:
    return None


def run_contradiction_detection(
    memory_id: UUID,
    tenant_id: str,
    fleet_id: str | None,
    *,
    trigger: Trigger,
    content: str | None = None,
    embedding: list[float] | None = None,
    new_memory: dict | None = None,
) -> Awaitable[None]:
    if settings.contradiction_engine_enabled:
        return ContradictionEngine().evaluate_async(
            memory_id,
            tenant_id,
            fleet_id,
            trigger=trigger,
            content=content,
            embedding=embedding,
            new_memory=new_memory,
        )

    # Legacy arch — identical to the direct calls the trigger sites made before
    # the engine seam existed. Invoked synchronously so the returned coroutine is
    # created at schedule time, exactly like the old ``detect_contradictions_async(...)``.
    from core_api.services import contradiction_detector as legacy

    if trigger is Trigger.ENTITY:
        return legacy.detect_contradictions_by_entities_async(memory_id, tenant_id, fleet_id)

    if trigger in _PATH_A:
        # No embedding -> nothing to do here (deferred to the EMBEDDED
        # back-channel), same as today. Return a no-op awaitable so callers can
        # schedule/await uniformly.
        if content is None or embedding is None:
            return _noop()
        return legacy.detect_contradictions_async(
            memory_id, tenant_id, fleet_id, content, embedding, new_memory=new_memory
        )

    raise ValueError(f"unknown contradiction trigger: {trigger!r}")
