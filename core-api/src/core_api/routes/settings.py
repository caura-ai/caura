"""Per-tenant settings endpoints."""

from fastapi import APIRouter, Depends, Header, HTTPException

from core_api import openapi_responses as _oar
from core_api.auth import AuthContext, get_auth_context
from core_api.services.organization_settings import (
    PROVIDER_OPTIONS,
    get_settings_for_display,
    update_settings,
)

router = APIRouter(tags=["Auth & Account"])


def _resolve_tenant(auth: AuthContext, tenant_id: str | None) -> str:
    """Admin can specify any tenant. Tenant users use their own.

    Keyed on ``auth.is_admin``, not on "has no tenant". Those are not the same
    set: the shared ``CAURA_API_KEY`` gate (auth Path 2) builds a tenant-less,
    NON-admin context when the request names no ``X-Tenant-ID`` — and because
    it names no tenant, the suppression guard on that path has nothing to
    check. Deriving admin from ``tenant_id is None`` let such a caller pick any
    tenant here via ``?tenant_id=``, past both the tenant binding and the
    suppression guard (2026-08-14 audit L-39). A tenant-less non-admin now
    falls through to the 400 below, like any other context without a tenant.
    """
    if auth.is_admin and tenant_id:
        return tenant_id
    if auth.tenant_id:
        return auth.tenant_id
    raise HTTPException(status_code=400, detail="tenant_id required")


@router.get("/settings", responses={200: {"model": _oar.SettingsResponse}})
async def get_tenant_settings(
    tenant_id: str | None = None,
    auth: AuthContext = Depends(get_auth_context),
):
    """Get tenant settings (API keys masked). Admin can query any tenant."""
    tid = _resolve_tenant(auth, tenant_id)
    return await get_settings_for_display(tid)


@router.put("/settings", responses={200: {"model": _oar.SettingsResponse}})
async def update_tenant_settings(
    body: dict,
    tenant_id: str | None = None,
    x_changed_by: str | None = Header(default=None, alias="X-Changed-By"),
    auth: AuthContext = Depends(get_auth_context),
):
    """Update tenant settings. Accepts partial updates. API keys are encrypted at rest.

    **Resetting a setting.** Updates are a deep MERGE, so omitting a key leaves
    it as it was — omission means "don't touch", never "clear". To return a
    setting to its default, send it explicitly as ``null``:

        {"search": {"recall_boost": null}}          # one leaf back to default
        {"search": {"default_profile": null}}       # a whole section back to default

    Sending ``{}`` for a section is a NO-OP, not a reset — an empty dict merges
    nothing. That reads like a clear and is the shape operators reach for first,
    so it is called out here rather than left to be discovered by a flip that
    silently did not take.

    **Propagation.** A write invalidates the handling worker's cache
    immediately and broadcasts ``Org.SETTINGS_CHANGED`` so sibling workers drop
    their copies (CAURA-571). Where the event bus is in-process — the default,
    including local dev — that broadcast does not cross processes, and siblings
    fall back to the 5-minute TTL. Expect propagation, not immediacy, when
    flipping a flag and measuring the result.

    Audit attribution: honours ``X-Changed-By`` only when the caller is
    authenticated via the admin API key (i.e. the enterprise admin-api proxy).
    Regular user requests always use ``auth.user_id`` regardless of the header.
    """
    tid = _resolve_tenant(auth, tenant_id)
    # ``enforce_read_only`` rather than the ``is_demo`` check this replaced: that
    # check caught the demo sandbox but NOT a credential minted read-only by
    # construction (capabilities={'read'}), so a viewer/reporting key could
    # rewrite tenant settings. Both signals live behind this one gate, which is
    # why write-shaped endpoints are supposed to call it instead of testing
    # individual flags.
    auth.enforce_read_only()
    # Deliberately NO ``enforce_usage_limits`` here — pinned by
    # ``test_settings_still_works_when_over_usage_limits`` so nobody "fixes" it.
    # Plan-limit read-only mode exists to stop an over-plan org GROWING the
    # store (see the policy record in ``services/usage_service.py``); settings
    # rows add nothing to it. And this is a mitigation route: an over-quota
    # tenant must still be able to turn enrichment off, rotate a leaked
    # provider key or require agent approval. Same carve-out, for the same
    # reason, as ``PATCH /agents/{id}/trust``. The 2026-08-14 audit (H-15)
    # named the missing call; the omission is the decision, not an oversight.
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
        return await update_settings(tid, body, changed_by=changed_by)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.get(
    "/settings/providers",
    responses={200: {"model": dict[str, dict[str, list[str]]]}},
)
async def list_providers():
    """List available LLM providers and models for each function."""
    return PROVIDER_OPTIONS
