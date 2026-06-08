"""Audit log endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.schemas import AUDIT_LOG_FIELDS, orm_to_dict
from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/audit-logs", tags=["Audit"])
_svc = PostgresService()

# Required keys on every audit event. Missing any of these used to
# raise ``KeyError`` deep in ``audit_add_batch`` (and silently lose
# every other event in the same drained batch); validating at the
# boundary turns a 500 into a 422 with an actionable index + field
# list, and protects already-drained valid events from collateral
# loss.
_REQUIRED_AUDIT_FIELDS = frozenset({"tenant_id", "action", "resource_type"})

# Hard ceiling on the bulk endpoint so a buggy or malicious caller
# can't bypass core-api's ``audit_queue_flush_threshold`` (50 default)
# and submit millions of rows in one INSERT. 500 is a generous
# multiple of the default flush_threshold and keeps any single INSERT
# bounded for predictable AlloyDB transaction time.
_MAX_BATCH_SIZE = 500


def _parse_resource_id(rid: object) -> UUID | None:
    """``UUID(rid)`` with a 422 on a malformed input.

    Pre-422 a malformed ``resource_id`` raised ``ValueError`` and
    propagated as a 500. On the bulk path that meant one bad event
    crashed the whole batch — every other valid event drained from
    the queue alongside it was lost.
    """
    if rid is None:
        return None
    try:
        return UUID(rid)  # type: ignore[arg-type]
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid resource_id UUID: {rid!r}",
        ) from exc


@router.post("")
async def create_audit_log(request: Request) -> dict:
    body: dict = await request.json()
    await _svc.audit_add(
        tenant_id=body["tenant_id"],
        agent_id=body.get("agent_id"),
        action=body["action"],
        resource_type=body["resource_type"],
        resource_id=_parse_resource_id(body.get("resource_id")),
        detail=body.get("detail"),
    )
    return {"ok": True}


@router.post("/bulk")
async def create_audit_logs_bulk(request: Request) -> dict:
    """Batched audit insert (CAURA-628).

    Accepts ``{"events": [<event>, ...]}`` and persists them in a
    single multi-row INSERT — one table-lock acquisition + one
    AlloyDB round-trip regardless of batch size. The legacy per-event
    endpoint (``POST /audit-logs``) kept for the synchronous-fallback
    path in core-api's ``log_action``; once core-api is on the queue
    path everywhere, the legacy endpoint can be retired in a separate
    cycle.

    Validation happens at the boundary so malformed events surface as
    422 before the database session opens — protects valid events in
    the same batch from being rolled back. Order:

      1. Reject non-list payloads.
      2. Reject batch sizes above ``_MAX_BATCH_SIZE``.
      3. Walk every event: collect required-field-missing AND
         malformed-``resource_id`` errors, then raise once with the
         full list. Caller fixes the whole bad set in one round-trip
         instead of having to play whack-a-mole one event at a time.
    """
    body: dict = await request.json()
    events = body.get("events") or []
    # Guard the type explicitly — without this, ``len(events)`` would
    # raise ``TypeError`` on a non-list (e.g. a caller mis-shaping the
    # payload as ``{"events": {"0": {...}}}``) and propagate as a 500.
    if not isinstance(events, list):
        raise HTTPException(
            status_code=422,
            detail=f"'events' must be a list, got {type(events).__name__!r}",
        )
    if len(events) > _MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=422,
            detail=(f"Batch too large: {len(events)} events (max {_MAX_BATCH_SIZE})"),
        )
    # Pass 1: collect all validation errors. Doing this in two passes
    # (validate, then normalise) means a caller submitting 50 events
    # with 3 bad fields gets all 3 errors back in one 422 — no
    # round-trip-per-fix.
    errors: list[dict] = []
    for i, event in enumerate(events):
        # Per-element type guard: the outer ``isinstance(events, list)``
        # check above only confirms the container is a list; JSON
        # arrays can hold arbitrary types, so a payload like
        # ``{"events": [null, "oops", 123]}`` would crash with
        # ``AttributeError`` on ``event.keys()`` below and propagate
        # as a 500. Surface as 422 with a typed error so the caller
        # can self-diagnose.
        if not isinstance(event, dict):
            errors.append(
                {
                    "index": i,
                    "error": (f"event must be an object, got {type(event).__name__!r}"),
                }
            )
            continue
        missing = _REQUIRED_AUDIT_FIELDS - event.keys()
        if missing:
            errors.append({"index": i, "missing_fields": sorted(missing)})
        rid = event.get("resource_id")
        if rid is not None:
            try:
                UUID(str(rid))
            except (ValueError, TypeError):
                errors.append({"index": i, "invalid_resource_id": repr(rid)})
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    # Pass 2: normalise. ``resource_id`` already validated above so
    # ``UUID(rid)`` here is just the type conversion — no error path.
    normalised: list[dict] = []
    for event in events:
        normalised_event = dict(event)
        rid = normalised_event.get("resource_id")
        if rid is not None:
            normalised_event["resource_id"] = UUID(rid)
        normalised.append(normalised_event)
    await _svc.audit_add_batch(normalised)
    return {"ok": True, "count": len(normalised)}


@router.get("")
async def list_audit_logs(
    tenant_id: str,
    limit: int = 50,
    offset: int = 0,
    action: str | None = None,
    resource_type: str | None = None,
) -> list[dict]:
    logs = await _svc.audit_list_by_tenant(tenant_id, limit=limit)
    results = [orm_to_dict(log, AUDIT_LOG_FIELDS) for log in logs]
    if action:
        results = [r for r in results if r.get("action") == action]
    if resource_type:
        results = [r for r in results if r.get("resource_type") == resource_type]
    if offset:
        results = results[offset:]
    return results
