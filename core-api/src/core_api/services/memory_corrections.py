"""Correction propagation, CP-08/CP-09: announce that a memory was corrected.

One hook, :func:`on_memory_corrected`, called after the write commits from the
four places a person corrects a memory:

- ``memory_service.update_memory`` when the content changed (``content_edit``),
  or when the update moved the status to a correction status (``status``);
- the status routes (REST ``PATCH /memories/{id}/status``, MCP op=transition)
  and the deletes (REST ``DELETE /memories/{id}``, MCP op=delete) for
  ``outdated``, ``cancelled`` or ``deleted`` (``status``);
- ``verification.change_verification`` withdrawing a ``human_verified``
  (``unverify``);
- ``PATCH /conflicts/{id}/resolve`` (``conflict_resolve``, via
  :func:`on_conflict_resolved`).

Who counts (decision D2): a person, an admin or tenant key acting for the
tenant, or an agent at ``trust_level >= 3``. Anything else is not a correction:
a lower-trust agent's edit, an install credential (a machine), and every path
with no request principal, which is how the contradiction detector and the
near-duplicate merge run. Those retire rows through storage directly and never
reach this module; the principal check is the second line.

What it does, off the request path and never failing it: one audit entry
``memory.corrected`` (without the text, as every memory audit row) and one
``caura.memory.corrected`` event carrying the summaries
(:class:`common.events.memory_corrected.MemoryCorrected`).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from common.events.memory_corrected import (
    CorrectionActor,
    CorrectionKind,
    MemoryCorrected,
    summarize,
)
from common.events.memory_corrected_publisher import publish_memory_corrected
from core_api.audit_actor import audit_principal
from core_api.tasks import track_task

logger = logging.getLogger(__name__)

MEMORY_CORRECTED_ACTION = "memory.corrected"

#: A status a person sets to say the memory was wrong or should go.
CORRECTION_STATUSES = frozenset({"outdated", "cancelled", "deleted"})

#: An agent corrects only at this trust level; below it, its edit is its own.
CORRECTION_MIN_TRUST = 3

# Credential kinds that are machines, not a person or the tenant's own key.
_MACHINE_CREDENTIAL_KINDS = frozenset({"install_credential", "system"})


def _actor(agent_id: str | None, principal: dict) -> CorrectionActor:
    if agent_id:
        return CorrectionActor(kind="agent", id=str(agent_id))
    if principal.get("user_id"):
        return CorrectionActor(kind="user", id=str(principal["user_id"]))
    return CorrectionActor(kind=str(principal.get("credential_kind") or "tenant_key"))


async def _qualifies(tenant_id: str, actor: CorrectionActor) -> bool:
    """D2: a person or tenant key always; an agent only at trust >= 3."""
    if actor.kind in _MACHINE_CREDENTIAL_KINDS:
        return False
    if actor.kind != "agent":
        return True
    from core_api.services.agent_service import lookup_agent

    agent = await lookup_agent(tenant_id, actor.id or "", read=False)
    return agent is not None and int(agent.get("trust_level") or 0) >= CORRECTION_MIN_TRUST


async def _emit(event: MemoryCorrected) -> None:
    try:
        if not await _qualifies(event.tenant_id, event.actor):
            return
        from core_api.services.audit_service import log_action

        await log_action(
            tenant_id=event.tenant_id,
            agent_id=event.actor.id if event.actor.kind == "agent" else None,
            action=MEMORY_CORRECTED_ACTION,
            resource_type="memory",
            resource_id=event.memory_id,
            detail={
                "kind": event.kind,
                "actor_kind": event.actor.kind,
                "actor_id": event.actor.id,
                "fleet_id": event.fleet_id,
                # Statuses and ids only; the summaries stay on the event.
                **event.detail,
            },
        )
        await publish_memory_corrected(event)
    except Exception:
        logger.warning("memory.corrected for %s not announced (non-critical)", event.memory_id, exc_info=True)


def on_memory_corrected(
    *,
    tenant_id: str,
    memory: dict,
    kind: CorrectionKind,
    agent_id: str | None,
    old_summary: str | None,
    new_summary: str | None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Announce that ``memory`` (the row before the change) was corrected.

    Call after the write committed. Returns at once: the trust check, the audit
    entry and the publish run in a background task, and any failure there is
    logged, never raised. A call with no request principal (a background job)
    announces nothing.
    """
    try:
        principal = audit_principal.get()
        if principal is None:
            return
        event = MemoryCorrected(
            memory_id=memory["id"],
            tenant_id=tenant_id,
            fleet_id=memory.get("fleet_id"),
            actor=_actor(agent_id, principal),
            kind=kind,
            corrected_at=datetime.now(UTC),
            old_summary=summarize(old_summary),
            new_summary=summarize(new_summary),
            detail=dict(detail or {}),
        )
        track_task(_emit(event))
    except Exception:
        logger.warning("memory.corrected hook failed (non-critical)", exc_info=True)


def on_status_corrected(
    *, tenant_id: str, memory: dict, old_status: str | None, new_status: str, agent_id: str | None
) -> None:
    """:func:`on_memory_corrected` for a status change, if it is a correction."""
    if new_status in CORRECTION_STATUSES and new_status != old_status:
        on_memory_corrected(
            tenant_id=tenant_id,
            memory=memory,
            kind="status",
            agent_id=agent_id,
            old_summary=old_status,
            new_summary=new_status,
            detail={"old_status": old_status, "new_status": new_status},
        )


async def _conflict_corrected(
    tenant_id: str,
    conflict: dict,
    review_status: str,
    resolution_action: str | None,
    agent_id: str | None,
) -> None:
    try:
        from core_api.clients.storage_client import get_storage_client

        sc = get_storage_client()
        old_id, new_id = str(conflict["old_memory_id"]), str(conflict["new_memory_id"])
        old = await sc.get_memory(old_id, tenant_id, read=False) or {"id": old_id}
        new = await sc.get_memory(new_id, tenant_id, read=False) or {"id": new_id}
        # The event names the pair's old memory: resolved, it is the one the
        # verdict corrected; dismissed, the one detection had wrongly retired
        # and the dismissal restores. The other is ``related_memory_id``.
        on_memory_corrected(
            tenant_id=tenant_id,
            memory=old,
            kind="conflict_resolve",
            agent_id=agent_id,
            old_summary=old.get("content"),
            new_summary=new.get("content") if review_status == "resolved" else None,
            detail={
                "conflict_id": str(conflict.get("id")),
                "review_status": review_status,
                "resolution_action": resolution_action,
                "related_memory_id": new_id,
            },
        )
    except Exception:
        logger.warning("memory.corrected for a conflict review failed (non-critical)", exc_info=True)


def on_conflict_resolved(
    *,
    tenant_id: str,
    conflict: dict,
    review_status: str,
    resolution_action: str | None,
    agent_id: str | None,
) -> None:
    """Announce a conflict review. Reads the pair off the request path."""
    if audit_principal.get() is None:
        return
    track_task(_conflict_corrected(tenant_id, conflict, review_status, resolution_action, agent_id))
