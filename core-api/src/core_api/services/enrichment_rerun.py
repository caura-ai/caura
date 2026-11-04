"""Enrich again the fast-mode memories an LLM governance policy could not judge.

In fast mode the tenant's LLM policies (free-form PII, business or personal) are
applied after enrichment, from the verdict it wrote. When every provider fails,
enrichment falls back to the keyword heuristic, which gives none, and
``governance_remediation`` marks the row ``governance_llm_uncertain`` and logs a
``background_task_log`` row of that task. This is the reader:
``rerun_unjudged_enrichments`` is the hourly sweep (core-operations'
``enrichment-rerun`` job, through ``POST /admin/enrichment/rerun-unjudged``).

It runs the same sweep as ``services.extraction_rerun`` (``sweep_open_tasks``):
the oldest open rows across tenants, a few re-runs at a time, at most
``RERUN_MAX_PER_MEMORY`` per memory. A re-run is the enrichment a write
schedules, through ``_schedule_enrich_or_inline`` with remediation on, so it
runs inline or on core-worker as the deployment does. Like a release
(``release_replay``), it keeps the fields the writer may have set. A verdict
clears the mark and applies the policy; another failure leaves a fresh row for
a later sweep.
"""

from __future__ import annotations

import logging
from uuid import UUID

from core_api.services.extraction_rerun import live_memory, rerun_slots, sweep_open_tasks
from core_api.services.governance_remediation import LLM_UNCERTAIN, can_judge_again, marked_uncertain
from core_api.services.memory_service import _schedule_enrich_or_inline
from core_api.services.organization_settings import resolve_config
from core_api.services.release_replay import release_pins
from core_api.services.system_metadata import caller_owned_keys, extract_system_metadata
from core_api.services.task_tracker import tracked_task
from core_api.tasks import track_task

logger = logging.getLogger(__name__)


def _metadata(memory: dict) -> dict:
    return memory.get("metadata_") or memory.get("metadata") or {}


async def _wanted(memory: dict, tenant_id: str) -> bool:
    """Still marked, an LLM policy still on, and a provider that could judge it.

    An edit, or a later run that got a verdict, has cleared the mark.
    """
    if not marked_uncertain(_metadata(memory)):
        return False
    config = await resolve_config(tenant_id)
    policy_on = config.governance_pii.enabled or config.governance_non_business.enabled
    return policy_on and can_judge_again(config)


async def _rerun(memory_id: str, tenant_id: str) -> None:
    """Enrich the memory again, with remediation, a few at a time.

    Read again once a slot is free, as the extraction re-run does: it can wait
    behind others, and an edit meanwhile runs its own enrichment.
    """
    async with rerun_slots():
        memory = await live_memory(memory_id, tenant_id)
        if memory is None or not marked_uncertain(_metadata(memory)):
            logger.info(
                "enrichment re-run: memory %s is gone, held or judged since; nothing to do", memory_id
            )
            return
        metadata = _metadata(memory)
        await _schedule_enrich_or_inline(
            UUID(memory_id),
            memory.get("content") or "",
            tenant_id,
            memory.get("fleet_id"),
            memory.get("agent_id") or "",
            await resolve_config(tenant_id),
            agent_provided_fields=release_pins(memory, extract_system_metadata(metadata) or {}),
            caller_owned_metadata_keys=sorted(caller_owned_keys(metadata)) or None,
            run_governance_remediation=True,
        )


def _schedule(memory_id: str, tenant_id: str) -> None:
    # The same task as the row it answers: a re-run that raises or is cancelled
    # records one, which a later sweep picks up.
    track_task(tracked_task(_rerun(memory_id, tenant_id), LLM_UNCERTAIN, UUID(memory_id), tenant_id))


async def rerun_unjudged_enrichments() -> dict:
    """The hourly sweep; counts as ``sweep_open_tasks`` returns them."""
    return await sweep_open_tasks(
        (LLM_UNCERTAIN,), wanted=_wanted, schedule=_schedule, label="enrichment re-run"
    )
