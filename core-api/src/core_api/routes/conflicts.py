"""D11 — human review of detected conflicts.

``memory_conflicts`` has recorded what the DETECTOR concluded since A55, and
nothing recorded what a PERSON concluded. That gap is why detector precision has
never been measurable: a false positive looks exactly like a true one in storage.

These routes are the read + decide half. They deliberately keep the detector's
proposal (``action``, ``audit_reason``) and the reviewer's decision
(``resolution_action``, ``resolution_note``) as separate fields, because
comparing them is the measurement — a ``dismissed`` row is the only record in the
system that detection was wrong.

Read-only listing plus one state transition; no memory row is touched here.
Applying a resolution to the memories themselves stays with the contradiction
engine, which already owns the CAS-guarded status/supersedes writes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from core_api import openapi_responses as _oar
from core_api.agent_ids import DEFAULT_AGENT_ID
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.errors import (
    AUTH_AGENT_NOT_REGISTERED,
    coded_detail,
)
from core_api.schemas import ConflictListResponse, ConflictOut, ConflictResolveRequest
from core_api.services.audit_service import log_action
from core_api.services.trust_service import require_trust as _require_trust

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


@router.patch("/conflicts/{conflict_id}/resolve", responses={200: {"model": _oar.ConflictOut}})
async def resolve_conflict(
    conflict_id: str,
    body: ConflictResolveRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> ConflictOut:
    """Record a reviewer's decision on one conflict.

    409 when the row is no longer ``pending``. Two people working the same queue
    is the ordinary case, not the edge case: the storage-side compare-and-set is
    what stops the second decision silently overwriting the first, and this is
    the status code that tells the caller their queue entry was stale rather
    than pretending the write landed.
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
    reviewer = auth.agent_id or DEFAULT_AGENT_ID
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
    try:
        row = await get_storage_client().resolve_memory_conflict(conflict_id, payload)
    except HTTPException:
        raise
    except Exception as exc:  # storage 4xx surfaced with its own status
        status = getattr(exc, "status_code", None) or getattr(
            getattr(exc, "response", None), "status_code", None
        )
        if status in (400, 404, 409):
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        raise
    await log_action(
        tenant_id=body.tenant_id,
        agent_id=auth.agent_id,
        action="conflict.review",
        resource_type="memory_conflict",
        resource_id=conflict_id,
        detail={
            "review_status": body.review_status,
            "resolution_action": body.resolution_action,
        },
    )
    return ConflictOut(**row)
