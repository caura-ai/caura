"""Insights REST endpoints — mirrors memclaw_insights MCP tool."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from core_api.auth import AuthContext, get_auth_context
from core_api.constants import INSIGHTS_FOCUS_MODES, VALID_SCOPES
from core_api.db.session import get_db
from core_api.services.audit_service import log_action
from core_api.services.trust_service import parse_trust_error, require_trust
from core_api.services.usage_service import check_and_increment_by_tenant as check_and_increment

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Insights"])


# ── Schemas ──


class InsightsRequest(BaseModel):
    tenant_id: str
    focus: str = Field(
        description=(
            "Analysis focus: 'contradictions', 'failures', 'stale', 'divergence', 'patterns', or 'discover'."
        ),
    )
    scope: str = Field(
        default="agent",
        description="Scope: 'agent' (your memories), 'fleet' (fleet-wide), or 'all' (tenant-wide).",
    )
    fleet_id: str | None = Field(
        default=None,
        description="Fleet to analyze (required when scope='fleet').",
    )
    agent_id: str | None = Field(
        default=None,
        description=(
            "Identifier of the requesting agent. Optional: the gateway-"
            "verified ``X-Agent-ID`` header takes precedence when present; "
            "falls back to 'mcp-agent' when both are absent."
        ),
    )

    @field_validator("scope")
    @classmethod
    def _valid_scope(cls, v: str) -> str:
        if v not in VALID_SCOPES:
            raise ValueError(f"Invalid scope '{v}'. Must be: {', '.join(VALID_SCOPES)}.")
        return v

    @model_validator(mode="after")
    def _scope_focus_coupling(self) -> InsightsRequest:
        if self.scope == "fleet" and not self.fleet_id:
            raise ValueError("fleet_id is required when scope is 'fleet'.")
        if self.focus == "divergence" and self.scope == "agent":
            raise ValueError("Focus 'divergence' requires scope='fleet' or scope='all'.")
        return self


# ── Routes ──


@router.post("/insights/generate")
async def generate_insights_endpoint(
    body: InsightsRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Generate insights over stored memories.

    Identity resolution mirrors the data-plane endpoints (write/search/recall):
    if the gateway stamped a verified ``X-Agent-ID`` header (``auth.agent_id``),
    that wins; otherwise ``body.agent_id`` is used. The resolved id must
    correspond to an existing agent in the tenant that meets the scope's
    trust requirement.

    Trust gating: scope='agent' requires trust ≥ 1, scope='fleet'/'all'
    requires trust ≥ 2. Admin keys bypass.
    """
    auth.enforce_tenant(body.tenant_id)
    auth.enforce_read_only()
    auth.enforce_usage_limits()

    # Validate inputs before consuming rate-limit budget. scope/fleet_id/focus
    # coupling lives on InsightsRequest via ``field_validator`` /
    # ``model_validator``; this body-level check only covers the focus enum,
    # which is inherently coupled to a constants table rather than a Literal.
    if body.focus not in INSIGHTS_FOCUS_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid focus '{body.focus}'. Must be one of: {', '.join(INSIGHTS_FOCUS_MODES)}",
        )

    # Gateway-verified identity wins when present; otherwise fall back to
    # body.agent_id, then to the "mcp-agent" placeholder so standalone
    # callers still show up in audit logs. Warn only when the client sent
    # an explicit body.agent_id that disagrees with the verified header —
    # the Pydantic default would fire spuriously on every gateway call.
    if auth.agent_id and body.agent_id is not None and auth.agent_id != body.agent_id:
        logger.warning(
            "insights: verified agent_id=%s overrides body.agent_id=%s",
            auth.agent_id,
            body.agent_id,
        )
    caller_agent_id = auth.agent_id or body.agent_id or "mcp-agent"

    # Admin bypass — super admins are tenant-free and trust enforcement
    # assumes a tenant-scoped agent row.
    if not auth.is_admin:
        if not caller_agent_id:
            raise HTTPException(status_code=422, detail="agent_id is required.")
        min_level = 1 if body.scope == "agent" else 2
        # ``require_trust`` soft-passes a missing Agent row at
        # ``DEFAULT_TRUST_LEVEL`` so read-only callers don't 403 on a
        # brand-new agent_id. Write paths still need identity pinning,
        # though: this endpoint persists insight memories plus an
        # audit-log entry keyed to ``caller_agent_id``, and without a
        # registered row backing the name, attribution becomes
        # unverifiable. API-key admission proves authorization, not
        # identity — re-block unregistered agents on writes
        # specifically. The soft-pass remains useful for read-only
        # consumers calling ``require_trust`` directly.
        _, not_found, terr = await require_trust(db, body.tenant_id, caller_agent_id, min_level=min_level)
        if not_found:
            raise HTTPException(
                status_code=403,
                detail=f"Agent '{caller_agent_id}' is not registered in tenant '{body.tenant_id}'.",
            )
        if terr:
            raise HTTPException(status_code=403, detail=parse_trust_error(terr))

    await check_and_increment(db, body.tenant_id, "insights")

    from core_api.services.insights_service import generate_insights

    result = await generate_insights(
        db,
        tenant_id=body.tenant_id,
        focus=body.focus,
        scope=body.scope,
        fleet_id=body.fleet_id,
        agent_id=caller_agent_id,
    )

    await log_action(
        db,
        tenant_id=body.tenant_id,
        action="insights_generate",
        resource_type="insight",
        detail={"focus": body.focus, "scope": body.scope, "agent_id": caller_agent_id},
    )
    await db.commit()

    return result
