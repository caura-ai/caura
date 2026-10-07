"""D11 — human review of detected conflicts.

``memory_conflicts`` has recorded what the DETECTOR concluded since A55, and
nothing recorded what a PERSON concluded. That gap is why detector precision has
never been measurable: a false positive looks exactly like a true one in storage.

These routes are the read + decide half. They deliberately keep the detector's
proposal (``action``, ``audit_reason``) and the reviewer's decision
(``resolution_action``, ``resolution_note``) as separate fields, because
comparing them is the measurement — a ``dismissed`` row is the only record in the
system that detection was wrong.

Read-only listing plus one state transition. A ``resolved`` decision touches no
memory row. A ``dismissed`` one also undoes what detection did to the pair
(M-102): it was the only record that detection was wrong, and nothing else ever
read it, so the loser stayed demoted and the winner's edge kept presenting it as
corrected. The undo goes through the same CAS-guarded writes retraction uses. A
verdict that left no chain edge (M-34) is known only from its record; its loser
is reverted when nothing else still holds it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from core_api import openapi_responses as _oar
from core_api.agent_ids import DEFAULT_AGENT_ID, canonical_service_agent_id
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.constants import CONTRADICTED_STATUSES
from core_api.errors import (
    AUTH_AGENT_NOT_REGISTERED,
    coded_detail,
)
from core_api.schemas import ConflictListResponse, ConflictOut, ConflictResolveRequest
from core_api.services.audit_service import log_action
from core_api.services.contradiction_detector import _pick_older, revert_unheld_loser
from core_api.services.trust_service import require_trust as _require_trust

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Conflicts"])

# Mirrors common.models.memory_conflict.REVIEW_STATUSES minus the implicit
# initial state, for the query-param filter.
_REVIEW_STATUSES = ("pending", "resolved", "dismissed")


@router.get("/conflicts", responses={200: {"model": _oar.ConflictListResponse}})
async def list_conflicts(
    tenant_id: str,
    review_status: str | None = Query(
        default=None,
        description="Filter by review state: pending | resolved | dismissed. Omit for all.",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_auth_context),
) -> ConflictListResponse:
    """The conflict review queue, oldest first.

    Oldest-first is deliberate: a queue worked newest-first leaves its oldest
    entries permanently at the bottom, which is precisely the backlog a reviewer
    needs to clear.
    """
    auth.enforce_tenant(tenant_id)
    if review_status is not None and review_status not in _REVIEW_STATUSES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid review_status '{review_status}'. Must be one of: " + ", ".join(_REVIEW_STATUSES),
        )
    rows = await get_storage_client().list_memory_conflicts(
        tenant_id=tenant_id, review_status=review_status, limit=limit, offset=offset
    )
    return ConflictListResponse(items=[ConflictOut(**r) for r in rows])


@router.get("/conflicts/{conflict_id}", responses={200: {"model": _oar.ConflictOut}})
async def get_conflict(
    conflict_id: str,
    tenant_id: str,
    auth: AuthContext = Depends(get_auth_context),
) -> ConflictOut:
    auth.enforce_tenant(tenant_id)
    row = await get_storage_client().get_memory_conflict(conflict_id, tenant_id)
    if row is None:
        # One 404 for "no such conflict" and "not your conflict" alike, so the
        # route cannot be used to probe which ids exist in other tenants.
        raise HTTPException(status_code=404, detail="Conflict not found")
    return ConflictOut(**row)


def _storage_status(exc: Exception) -> int | None:
    """The HTTP status a storage call failed with, when it carried one."""
    return getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)


async def _undo_dismissed_verdict(sc, conflict: dict) -> str:
    """Clear the edge detection wired between a dismissed pair and revert its loser.

    Direction-aware like retraction: the new memory owns the edge in a canonical
    verdict, the old one in a flipped verdict. Every read goes to the writer,
    because each decides a write made straight after it.

    The edge goes first, under a CAS that expects it to still point at the loser:
    that is what proves the verdict is still the one being dismissed, so a chain
    another writer has moved on since is never rewritten. (The clear sets the
    owner's status in the same statement, to the value just read, as retraction's
    does.) The loser is then read again and reverted only if detection's status is
    still on it and no other row still points at it. Storage has no expected-status
    guard on that write, so the late read is the narrowest window available.

    A pair no edge joins goes to ``_revert_unlinked_loser``.

    Returns ``"undone"``; ``"not_applied"`` when there was nothing to undo (a
    memory is gone, the CAS found the chain moved, or an unlinked loser is still
    held); or ``"partial"`` when the edge was cleared but the loser could not be
    reverted. A dismissed conflict cannot be filed again, so a partial undo is
    left to a person, who can set the loser's status through
    ``PATCH /memories/{id}``. Any other storage error, at or before the edge write,
    is raised: the caller records that as ``"failed"``.
    """
    tenant_id = str(conflict["tenant_id"])
    new_id, old_id = str(conflict["new_memory_id"]), str(conflict["old_memory_id"])
    new = await sc.get_memory(new_id, tenant_id, read=False)
    old = await sc.get_memory(old_id, tenant_id, read=False)
    if not new or not old:
        return "not_applied"
    if str(new.get("supersedes_id") or "") == old_id:
        owner_id, loser_id, owner_status = new_id, old_id, new.get("status", "active")
    elif str(old.get("supersedes_id") or "") == new_id:
        owner_id, loser_id, owner_status = old_id, new_id, old.get("status", "active")
    else:
        return await _revert_unlinked_loser(sc, conflict, new, old)
    try:
        await sc.update_memory_status(
            owner_id,
            owner_status,
            tenant_id=tenant_id,
            unset_supersedes=True,
            expected_supersedes_id=loser_id,
        )
    except Exception as exc:
        if _storage_status(exc) != 409:
            raise
        logger.warning("dismissal left the edge on %s: another writer has moved the chain", owner_id)
        return "not_applied"
    try:
        loser = await sc.get_memory(loser_id, tenant_id, read=False)
        # Another winner, or a later verdict, still marks the loser corrected.
        holders = await sc.find_by_supersedes_id(tenant_id, loser_id, read=False)
        holders = [r for r in holders if str(r["id"]) != owner_id]
        if (
            loser
            and loser.get("deleted_at") is None
            and loser.get("status") in CONTRADICTED_STATUSES
            and not holders
        ):
            await sc.update_memory_status(loser_id, "active", tenant_id=tenant_id)
    except Exception:
        logger.error(
            "dismissal cleared the edge on %s but could not revert %s; set its status by hand",
            owner_id,
            loser_id,
            exc_info=True,
        )
        return "partial"
    return "undone"


async def _revert_unlinked_loser(sc, conflict: dict, new: dict, old: dict) -> str:
    """Revert the loser of a dismissed verdict no chain edge records (M-102, M-34).

    Detection demotes every loser of a run but wires a winner's one
    ``supersedes_id`` to its first loser only, or to none when it already
    supersedes a row. caura PR #1815 records each such loser, and that record is
    all that names it. A pair whose winner's edge has moved on since looks the
    same, so it is handled the same way.

    The loser is the older row, as detection picks it (``_pick_older``). It is
    reverted only when nothing else holds it: no row's ``supersedes_id`` points
    at it, and no other record that is not dismissed says it lost to a live,
    newer row. Then it is read again, last, and reverted only if detection's
    status is still on it, as the edge path does. Every read goes to the writer.
    The rule is ``revert_unheld_loser``'s, which a winner's content edit and
    Path C's retraction apply too.
    Returns ``"undone"`` or ``"not_applied"``. A storage error is raised, for the
    caller to record as ``"failed"``: nothing has been written.
    """
    tenant_id, conflict_id = str(conflict["tenant_id"]), str(conflict.get("id"))
    reverted = await revert_unheld_loser(
        sc, tenant_id, _pick_older(new, old), ignore=lambda r: str(r.get("id")) == conflict_id
    )
    return "undone" if reverted else "not_applied"


@router.patch("/conflicts/{conflict_id}/resolve", responses={200: {"model": _oar.ConflictOut}})
async def resolve_conflict(
    conflict_id: str,
    body: ConflictResolveRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> ConflictOut:
    """Record a reviewer's decision on one conflict.

    ``dismissed`` also undoes detection's change to the pair: the winner's
    ``supersedes_id`` is cleared and the loser reverted to ``active``, unless the
    chain has changed since, someone else has set the loser's status, or another
    row still points at it. For a pair no edge joins, the older row is reverted
    when no edge and no other standing record still holds it (M-102). The
    decision is recorded either way, and the audit entry's ``undo`` says how far
    the undo got: ``undone``, ``not_applied``, ``partial``, or ``failed`` when a
    storage error stopped it at or before the edge write.

    404 when storage has no such conflict for the tenant (L-228). 409 when the
    row is no longer ``pending``. Two people working the same queue is the
    ordinary case, not the edge case: the storage-side compare-and-set is what
    stops the second decision silently overwriting the first, and this is the
    status code that tells the caller their queue entry was stale rather than
    pretending the write landed.
    """
    auth.enforce_tenant(body.tenant_id)
    # Blocks demo-sandbox and read-only credentials before any state moves — a
    # review decision is a write, even though no memory row changes.
    auth.enforce_read_only()
    # TRUST-plane, not admin-plane. ``enforce_admin`` admits only the super-admin
    # key, which would put review out of reach of the people who actually run a
    # tenant. But leaving this at tenant scope alone would let ANY agent
    # credential dismiss the contradictions it caused — and a dismissal is the
    # system's only record of the detector being wrong, so a self-serving one
    # corrupts the exact ground truth this surface exists to collect. Trust >= 2
    # matches the keystone-author bar: a privileged action inside a tenant.
    reviewer = canonical_service_agent_id(auth.agent_id or DEFAULT_AGENT_ID)
    _trust, not_found, terr = await _require_trust(body.tenant_id, reviewer, min_level=2)
    if not_found or terr:
        raise HTTPException(
            status_code=403,
            detail=coded_detail(
                AUTH_AGENT_NOT_REGISTERED,
                f"Agent '{reviewer}' cannot review conflicts. Reviewing records ground "
                "truth about detector accuracy, so it requires a registered agent at "
                "trust >= 2 — call with X-Agent-ID or an agent-scoped credential.",
            ),
        )
    payload = {
        "tenant_id": body.tenant_id,
        "review_status": body.review_status,
        "resolution_action": body.resolution_action,
        "resolution_note": body.resolution_note,
        # Attribution comes from the verified identity, never the body — a
        # reviewer must not be able to file a decision under someone else's name.
        "resolved_by": reviewer,
    }
    sc = get_storage_client()
    try:
        row = await sc.resolve_memory_conflict(conflict_id, payload)
    except HTTPException:
        raise
    except Exception as exc:  # storage 4xx surfaced with its own status
        status = _storage_status(exc)
        if status in (400, 404, 409):
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        raise
    if row is None:
        # L-228: storage's answer for an unknown or foreign id. Nothing was
        # decided, so there is nothing to undo and nothing to audit.
        raise HTTPException(status_code=404, detail="Conflict not found")
    # After the record: the decision stands even if undoing it does not.
    undo: str | None = None
    if body.review_status == "dismissed":
        try:
            undo = await _undo_dismissed_verdict(sc, row)
        except Exception:
            # A storage error at or before the edge write, after the client's own
            # retries. The verdict is most likely still in place (a timed-out write
            # may have landed), and the decision cannot be filed again, so it needs
            # a person.
            logger.error(
                "dismissal of conflict %s recorded but its verdict may still be in place",
                conflict_id,
                exc_info=True,
            )
            undo = "failed"
    await log_action(
        tenant_id=body.tenant_id,
        agent_id=auth.agent_id,
        action="conflict.review",
        resource_type="memory_conflict",
        resource_id=conflict_id,
        detail={
            "review_status": body.review_status,
            "resolution_action": body.resolution_action,
            "undo": undo,
            **auth.audit_actor(),
        },
    )
    return ConflictOut(**row)
