"""Run what a held write skipped, once a person releases it (g2.8).

A held write is stored, but the work that follows a write reaches other rows,
so it waits: the passes that read their row back skip a held one, and the
atomic-fact fan-out keeps its facts on the row instead of making children.
Released, the memory gets that work, as if it had been written live:

- **Never enriched** (a fast write whose background enrichment read the row
  back and found it held): enrichment runs now, as for a fast write, and
  applies the governance verdict and fans out the facts itself.
- **Already enriched**, inline (a strong write) or by the worker while it was
  held: what the ENRICHED consumer does next. The governance verdict, except for
  a strong write, which ``GovernanceDecision`` governed before it was written;
  then the facts kept on the row become children.
- **Either way:** the near-duplicate merge the write meant to make, entity
  extraction, and contradiction detection.

Rejecting a held write runs none of this.
"""

from __future__ import annotations

import logging
from uuid import UUID

from common.events.memory_enriched import MemoryEnriched
from core_api.clients.storage_client import get_storage_client
from core_api.services.system_metadata import caller_owned_keys, extract_system_metadata
from core_api.services.task_tracker import record_task_failure

logger = logging.getLogger(__name__)

#: The release route's ``tracked_task`` name, and what a failed step records
#: under: the replay goes on past a failed step, so that wrapper never sees it
#: raise and would record nothing (M-03).
REPLAY_TASK = "release_replay"

# Fields nothing but the writer can have set on a row enrichment hasn't reached.
_KEPT_IF_SET = ("title", "ts_valid_start", "ts_valid_end")


def release_pins(memory: dict, system: dict) -> list[str]:
    """The enrichment fields a released write keeps: the ones its writer set.

    The row records which: ``MergeEnrichmentFields`` noted whether the caller
    chose the type (``memory_type_agent_set``) and where the weight came from
    (``weight_source``). A row that doesn't say keeps both. And on a row no
    enrichment has reached, a title or date can only be the writer's.
    ``status`` is always kept: enrichment never sets it (CAURA-719).
    """
    pins = {"status"}
    if system.get("memory_type_agent_set") is not False:
        pins.add("memory_type")
    if system.get("weight_source") not in ("llm", "default"):
        pins.add("weight")
    pins.update(field for field in _KEPT_IF_SET if memory.get(field) is not None)
    return sorted(pins)


async def replay_released_write(memory_id: str, tenant_id: str) -> None:
    """Run, for a memory a person just released, the work its held write skipped.

    A step that fails is logged and recorded, and the steps after it still run.
    Reading the row and the tenant's settings aren't steps: without them there
    is nothing to run, so their failure raises, for ``tracked_task`` to record.
    """
    sc = get_storage_client()
    memory = await sc.get_memory(memory_id, tenant_id, read=False)
    if memory is None:
        # Deleted since the release: nothing left to replay onto.
        return
    metadata = memory.get("metadata_") or {}
    system = extract_system_metadata(metadata) or {}
    content = memory.get("content") or ""
    fleet_id = memory.get("fleet_id")
    agent_id = memory.get("agent_id") or ""

    from core_api.services.organization_settings import resolve_config

    config = await resolve_config(tenant_id)

    if not await _enrich_or_govern(sc, memory, metadata, system, config, memory_id, tenant_id):
        return  # the governance verdict dropped it

    candidate = system.get("near_duplicate_of")
    # The pending decision (L-17), or ``near_duplicate_merged`` on a row held
    # before L-17, which recorded the decision under that name.
    if candidate and (system.get("near_duplicate_merge_pending") or system.get("near_duplicate_merged")):
        from core_api.pipeline.steps.write.schedule_background_tasks import _merge_near_duplicate

        await _merge_near_duplicate(memory_id, str(candidate), tenant_id)

    if config.entity_extraction_enabled:
        from core_api.services.entity_extraction_worker import process_entity_extraction

        try:
            await process_entity_extraction(
                UUID(memory_id), tenant_id, fleet_id, agent_id, content, memory.get("memory_type") or "fact"
            )
        except Exception as exc:
            await _step_failed("entity extraction", memory_id, tenant_id, exc)

    embedding = memory.get("embedding")
    if embedding:
        # Without one yet, the EMBEDDED back-channel runs it when the vector
        # lands, as for any write: the row is live by then. No ``new_memory``:
        # the detector reads the row back itself, and an enrichment that ran
        # above may have applied a governance drop this function can't see.
        from core_api.services.contradiction import Trigger, run_contradiction_detection

        await run_contradiction_detection(
            UUID(memory_id),
            tenant_id,
            fleet_id,
            trigger=Trigger.WRITE,
            content=content,
            embedding=embedding,
        )


async def _enrich_or_govern(sc, memory, metadata, system, config, memory_id, tenant_id) -> bool:
    """Enrichment, or what follows it; ``False`` when governance dropped the row."""
    if system.get("enrichment_pending"):
        from core_api.services.memory_service import _schedule_enrich_or_inline

        try:
            await _schedule_enrich_or_inline(
                UUID(memory_id),
                memory.get("content") or "",
                tenant_id,
                memory.get("fleet_id"),
                memory.get("agent_id") or "",
                config,
                agent_provided_fields=release_pins(memory, system),
                caller_owned_metadata_keys=sorted(caller_owned_keys(metadata)) or None,
                # No ``GovernanceDecision`` ran for a fast write's text.
                run_governance_remediation=True,
            )
        except Exception as exc:
            await _step_failed("enrichment", memory_id, tenant_id, exc)
        return True

    from core_api.consumer import _fan_out_persisted_atomic_facts
    from core_api.services.governance_remediation import (
        GovernanceCascadeError,
        RemediationOutcome,
        remediate_after_enrichment,
    )

    outcome = RemediationOutcome()
    if system.get("write_mode") != "strong":
        try:
            outcome = await remediate_after_enrichment(memory, config)
        except GovernanceCascadeError as exc:
            # The row's own verdict applied, but rows derived from it still carry
            # what the policy forbade, and ``remediate_after_enrichment`` asks a
            # caller about to derive more to stop: no children, then, though the
            # ENRICHED consumer makes them for a row the verdict kept.
            await _step_failed("governance cleanup of derived rows", memory_id, tenant_id, exc)
            return not exc.outcome.dropped
        except Exception as exc:
            # Fail closed: no children for a row whose verdict didn't apply.
            await _step_failed("governance remediation", memory_id, tenant_id, exc)
            return True
        if outcome.dropped:
            return False
    payload = MemoryEnriched(memory_id=UUID(memory_id), tenant_id=tenant_id, content=memory["content"])
    await _fan_out_persisted_atomic_facts(sc, memory, payload, outcome)
    return True


async def _step_failed(step: str, memory_id: str, tenant_id: str, exc: Exception) -> None:
    """Log one step's failure and record it; called inside its ``except``."""
    logger.exception("release replay: %s failed for memory %s", step, memory_id)
    await record_task_failure(
        REPLAY_TASK,
        UUID(memory_id),
        tenant_id,
        RuntimeError(f"{step} failed: {type(exc).__name__}: {exc}"),
    )
