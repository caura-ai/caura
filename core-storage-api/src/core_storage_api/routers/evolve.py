"""Evolve scope-filter read + atomic weight-adjust/backfill write (Fix 2 Ph5b, PR2).

Routes the two raw-DB passes the core-api evolve service held against
``memories`` (services/evolve_service.py ``_filter_by_scope`` SELECT and the
``_ADJUST_WEIGHTS_BULK_SQL`` CTE + ``_BACKFILL_RULE_OUTCOME_SQL`` UPDATE)
through core-storage-api HTTP so core-api stops holding a direct ``db.execute``
for the evolve adapt step.

Two endpoints:

- ``POST /evolve/filter-by-scope`` (READ) — the scope SELECT; returns
  ``{allowed_ids}``. The UUID-parse + first-seen dedup + ``out_of_scope_count``
  arithmetic stays client-side in ``_filter_by_scope``.
- ``POST /evolve/apply-weights`` (WRITE, ONE atomic txn) — folds the weight
  clamp CTE AND the rule→outcome ``jsonb_set`` backfill into ONE transaction so
  evolve's documented split-commit isn't widened into two HTTP calls. Returns
  ``{adjustments, backfilled}``.

Every endpoint takes an explicit ``tenant_id`` (422 if missing) and scopes all
SQL by it — there are NO RLS GUCs server-side (mirrors the sibling routers).
The PostgresService methods these call port the source ORM/SQL VERBATIM.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.routers._validation import _require, _require_number
from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(tags=["Evolve"])
_svc = PostgresService()

# Mirrors core-api's VALID_SCOPES. Inlined (not imported) because core-storage-api
# is an independent service and must not depend on the core-api package. Keep in
# sync if the scope vocabulary changes.
_VALID_SCOPES = {"agent", "fleet", "all"}


@router.post("/evolve/filter-by-scope")
async def evolve_filter_by_scope(request: Request) -> dict:
    """Return the subset of ``ids`` the caller can touch under ``scope``.

    Body ``{tenant_id, caller_agent_id, scope, ids:[...], fleet_id?}``. Returns
    ``{allowed_ids:[str,...]}``. Returns 422 when ``scope='fleet'`` and
    ``fleet_id`` is absent, or when ``ids`` contains a non-UUID string — the
    underlying service raises ``ValueError`` on both, so guarding here gives
    direct storage callers a proper validation error instead of an unhandled
    500."""
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    caller_agent_id = _require(body, "caller_agent_id")
    scope = _require(body, "scope")
    if scope not in _VALID_SCOPES:
        # Fail closed: an unrecognized scope must 422, not fall through to the
        # service's no-extra-filter (scope='all') arm, which would return all
        # tenant-visible ids — a fail-open leak for a direct storage caller.
        raise HTTPException(status_code=422, detail=f"scope must be one of {sorted(_VALID_SCOPES)}")
    fleet_id = body.get("fleet_id")
    if scope == "fleet" and not fleet_id:
        raise HTTPException(status_code=422, detail="fleet_id is required when scope is 'fleet'")
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise HTTPException(status_code=422, detail="ids (list) is required")
    try:
        allowed = await _svc.evolve_filter_by_scope(
            tenant_id=tenant_id,
            caller_agent_id=caller_agent_id,
            fleet_id=fleet_id,
            scope=scope,
            ids=ids,
        )
    except ValueError as exc:
        # The service parses each id with ``UUID(s)`` — a non-UUID string raises
        # ValueError. Surface it as a 422 for direct storage callers rather than
        # an unhandled 500 (the typed client always sends pre-validated UUIDs;
        # same boundary-hardening as the fleet_id guard above).
        raise HTTPException(status_code=422, detail=f"invalid ids: {exc}") from exc
    return {"allowed_ids": allowed}


@router.post("/evolve/apply-weights")
async def evolve_apply_weights(request: Request) -> dict:
    """Clamp-and-adjust weights and (atomically) backfill the rule→outcome link.

    Body ``{tenant_id, ids:[...], delta, floor, cap, rule_id?, outcome_id?}``.
    Returns ``{adjustments:[{id, old_weight, new_weight}], backfilled: bool}``.
    The clamp CTE and the conditional ``jsonb_set`` backfill commit in ONE
    transaction."""
    body: dict = await request.json()
    tenant_id = _require(body, "tenant_id")
    ids = body.get("ids")
    if not isinstance(ids, list):
        raise HTTPException(status_code=422, detail="ids (list) is required")
    delta = _require_number(body, "delta")
    floor = _require_number(body, "floor")
    cap = _require_number(body, "cap")
    return await _svc.evolve_apply_weights(
        tenant_id=tenant_id,
        ids=ids,
        delta=delta,
        floor=floor,
        cap=cap,
        rule_id=body.get("rule_id"),
        outcome_id=body.get("outcome_id"),
    )
