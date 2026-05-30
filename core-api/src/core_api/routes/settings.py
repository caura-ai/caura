"""Per-tenant settings endpoints."""

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core_api.auth import AuthContext, get_auth_context
from core_api.db.session import get_db
from core_api.services.organization_settings import (
    PROVIDER_OPTIONS,
    get_settings_for_display,
    update_settings,
)

router = APIRouter(tags=["Auth & Account"])


def _resolve_tenant(auth: AuthContext, tenant_id: str | None) -> str:
    """Admin (tenant_id=None) can specify any tenant. Tenant users use their own."""
    is_admin = auth.tenant_id is None and not auth.is_demo
    if is_admin and tenant_id:
        return tenant_id
    if auth.tenant_id:
        return auth.tenant_id
    raise HTTPException(status_code=400, detail="tenant_id required")


@router.get("/settings")
async def get_tenant_settings(
    tenant_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
):
    """Get tenant settings (API keys masked). Admin can query any tenant."""
    tid = _resolve_tenant(auth, tenant_id)
    return await get_settings_for_display(db, tid)


@router.put("/settings")
async def update_tenant_settings(
    body: dict,
    tenant_id: str | None = None,
    x_changed_by: str | None = Header(default=None, alias="X-Changed-By"),
    db: AsyncSession = Depends(get_db),
    auth: AuthContext = Depends(get_auth_context),
):
    """Update tenant settings. Accepts partial updates. API keys are encrypted at rest.

    Audit attribution: honours ``X-Changed-By`` only when the caller is
    authenticated via the admin API key (i.e. the enterprise admin-api proxy).
    Regular user requests always use ``auth.user_id`` regardless of the header.
    """
    tid = _resolve_tenant(auth, tenant_id)
    if auth.is_demo:
        raise HTTPException(status_code=403, detail="Demo sandbox is read-only")
    # Tenant settings include security-relevant toggles (e.g. require_agent_approval,
    # which governs whether new agents start quarantined). An agent-scoped
    # credential must not be able to flip them.
    auth.enforce_not_agent_credential("change tenant settings")
    # Only trust X-Changed-By from admin-key callers (the enterprise proxy).
    # Regular users could forge this header otherwise.
    changed_by: str | None
    if x_changed_by and auth.is_admin:
        changed_by = x_changed_by
    else:
        changed_by = auth.user_id
    # StandaloneTenantMiddleware injects tenant_id into the JSON body — strip it
    # so the allowlist check in update_settings doesn't reject it.
    body.pop("tenant_id", None)
    try:
        return await update_settings(db, tid, body, changed_by=changed_by)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get("/settings/providers")
async def list_providers():
    """List available LLM providers and models for each function."""
    return PROVIDER_OPTIONS
