"""Organization-settings endpoints (Fix 2, Phase 0).

Moves the per-org settings read/write off core-api's direct DB pool and
behind core-storage-api, per the "no DB outside core-storage-api" rule. The
DB-touching half of ``core_api.services.organization_settings`` calls these:

* ``GET``  returns the raw override JSONB (``{}`` when unset).
* ``POST`` performs the transactional upsert — ``FOR UPDATE`` read → flat
  diff → JSONB ``||`` merge → append an audit row — all in one transaction so
  the lost-update guard holds. core-api keeps the TTL cache, the
  ``Org.SETTINGS_CHANGED`` publish, the schema validators, and ``DEFAULT_SETTINGS``
  resolution client-side; only the database access moves here.

POST (not PUT) so it rides the storage_client's non-idempotent ``_post`` path
(connection-phase retry only — a write whose response was lost is never
replayed). Re-applying an identical payload is a no-op anyway (the diff is
empty → no write, no audit row).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from core_storage_api.services.postgres_service import PostgresService

router = APIRouter(prefix="/organization-settings", tags=["Settings"])
_svc = PostgresService()


@router.get("/{org_id}")
async def get_organization_settings(org_id: str) -> dict:
    """Return ``{"settings": <raw overrides>}`` — ``{}`` when the org has none."""
    settings = await _svc.organization_settings_get(org_id)
    return {"settings": settings}


@router.post("/{org_id}")
async def update_organization_settings(org_id: str, request: Request) -> dict:
    """Transactional upsert + audit. Body: ``{settings, changed_by?}``.

    Returns ``{"settings": <merged overrides>, "changed": bool}``. ``changed``
    is ``False`` when the payload introduced no actual change (no row written).
    """
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=422, detail="request body must be valid JSON") from exc
    if not isinstance(body, dict):
        # Valid JSON can still be a list/scalar; guard before .get() so a
        # non-object body returns a clean 422 instead of an AttributeError 500.
        raise HTTPException(status_code=422, detail="request body must be a JSON object")
    new_settings = body.get("settings")
    if not isinstance(new_settings, dict):
        raise HTTPException(status_code=422, detail="'settings' must be an object")
    changed_by = body.get("changed_by")
    if changed_by is not None and not isinstance(changed_by, str):
        # changed_by lands in OrganizationSettingsAudit.changed_by (Text); reject
        # non-strings here rather than surfacing a confusing DB type error.
        raise HTTPException(status_code=422, detail="'changed_by' must be a string or null")
    return await _svc.organization_settings_update(
        org_id=org_id,
        new_settings=new_settings,
        changed_by=changed_by,
    )
