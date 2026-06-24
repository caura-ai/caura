"""Insights REST endpoints — mirrors memclaw_insights MCP tool."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from core_api.auth import AuthContext, get_auth_context
from core_api.constants import INSIGHTS_FOCUS_MODES, VALID_SCOPES
from core_api.services.audit_service import log_action
from core_api.services.caller_identity import resolve_caller_and_gate
from core_api.services.usage_service import check_and_increment_by_tenant as check_and_increment

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

    # Resolve the caller identity (verified > body > DEFAULT_AGENT_ID) and gate
    # the write on trust. Shared with evolve.py — see services/caller_identity.
    # Fix 2 Ph5b: all DB access for insights is storage-routed, so this route
    # holds no session — ``None`` is forwarded to the db-ignoring helpers
    # (``resolve_caller_and_gate`` only passes db to the storage-routed
    # ``require_trust``; ``check_and_increment`` / ``log_action`` ignore db).
    caller_agent_id = await resolve_caller_and_gate(
        auth,
        tenant_id=body.tenant_id,
        body_agent_id=body.agent_id,
        scope=body.scope,
        action="insights",
    )

    await check_and_increment(body.tenant_id, "insights")

    from core_api.services.insights_service import generate_insights

    result = await generate_insights(
        tenant_id=body.tenant_id,
        focus=body.focus,
        scope=body.scope,
        fleet_id=body.fleet_id,
        agent_id=caller_agent_id,
    )

    await log_action(
        tenant_id=body.tenant_id,
        action="insights_generate",
        resource_type="insight",
        detail={"focus": body.focus, "scope": body.scope, "agent_id": caller_agent_id},
    )

    return result
