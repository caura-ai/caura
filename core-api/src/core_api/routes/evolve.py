"""Evolve REST endpoints — mirrors memclaw_evolve MCP tool."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from core_api.auth import AuthContext, get_auth_context
from core_api.constants import EVOLVE_OUTCOME_TYPES, VALID_SCOPES
from core_api.db.session import get_db
from core_api.services.audit_service import log_action
from core_api.services.trust_service import parse_trust_error, require_trust
from core_api.services.usage_service import check_and_increment_by_tenant as check_and_increment

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Evolve"])


# ── Schemas ──


class EvolveRequest(BaseModel):
    tenant_id: str
    outcome: str = Field(
        min_length=1,
        description="What happened — natural language description of the outcome.",
    )
    outcome_type: str = Field(
        description="Result type: 'success', 'failure', or 'partial'.",
    )
    related_ids: list[str] | None = Field(
        default=None,
        description=(
            "Memory UUIDs that influenced your action. Use IDs from your most recent memclaw_recall results."
        ),
    )
    scope: str = Field(
        default="agent",
        description=(
            "Scope: 'agent' (default, requires trust ≥ 1, touches caller-owned memories only), "
            "'fleet' (requires trust ≥ 2, touches memories in fleet_id), or "
            "'all' (requires trust ≥ 2, tenant-wide)."
        ),
    )
    agent_id: str | None = Field(
        default=None,
        description=(
            "Identifier of the reporting agent. Optional: the gateway-"
            "verified ``X-Agent-ID`` header takes precedence when present; "
            "falls back to 'mcp-agent' when both are absent."
        ),
    )
    fleet_id: str | None = Field(
        default=None,
        description="Required when scope='fleet'; also used as the outcome/rule memory's fleet_id.",
    )

    @field_validator("outcome")
    @classmethod
    def _strip_and_require(cls, v: str) -> str:
        """Reject whitespace-only outcomes. Pydantic's ``min_length`` counts
        characters and would otherwise let ``"   "`` through, which the MCP
        handler correctly rejects."""
        stripped = (v or "").strip()
        if not stripped:
            raise ValueError("outcome must be a non-empty description.")
        return stripped

    @field_validator("scope")
    @classmethod
    def _valid_scope(cls, v: str) -> str:
        if v not in VALID_SCOPES:
            raise ValueError(f"Invalid scope '{v}'. Must be: {', '.join(VALID_SCOPES)}.")
        return v

    @model_validator(mode="after")
    def _fleet_required_when_scope_fleet(self) -> EvolveRequest:
        if self.scope == "fleet" and not self.fleet_id:
            raise ValueError("fleet_id is required when scope is 'fleet'.")
        return self


# ── Routes ──


@router.post("/evolve/report")
async def report_outcome_endpoint(
    body: EvolveRequest,
    auth: AuthContext = Depends(get_auth_context),
    db: AsyncSession = Depends(get_db),
):
    """Report an action outcome to evolve memory weights and generate rules.

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

    # Validate inputs before consuming rate-limit budget. scope + fleet_id
    # coupling and outcome stripping live on EvolveRequest via
    # ``field_validator`` / ``model_validator``; this body-level check
    # covers outcome_type, which is inherently coupled to a constants table
    # rather than a Literal.
    if body.outcome_type not in EVOLVE_OUTCOME_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid outcome_type '{body.outcome_type}'. "
                f"Must be one of: {', '.join(EVOLVE_OUTCOME_TYPES)}"
            ),
        )

    # Gateway-verified identity wins when present; otherwise fall back to
    # body.agent_id, then to the "mcp-agent" placeholder so standalone
    # callers still show up in audit logs. Warn only when the client sent
    # an explicit body.agent_id that disagrees with the verified header —
    # the Pydantic default would fire spuriously on every gateway call.
    if auth.agent_id and body.agent_id is not None and auth.agent_id != body.agent_id:
        logger.warning(
            "evolve: verified agent_id=%s overrides body.agent_id=%s",
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
        # though: this endpoint persists outcome and rule memories
        # plus an audit-log entry keyed to ``caller_agent_id``, and
        # without a registered row backing the name, attribution
        # becomes unverifiable (a tenant-key holder could synthesise
        # ``"admin-bot"`` and have that string appear in the audit
        # trail). API-key admission proves authorization, not identity
        # — re-block unregistered agents on writes specifically. The
        # soft-pass remains useful for read-only consumers calling
        # ``require_trust`` directly.
        _, not_found, terr = await require_trust(db, body.tenant_id, caller_agent_id, min_level=min_level)
        if not_found:
            raise HTTPException(
                status_code=403,
                detail=f"Agent '{caller_agent_id}' is not registered in tenant '{body.tenant_id}'.",
            )
        if terr:
            raise HTTPException(status_code=403, detail=parse_trust_error(terr))

    await check_and_increment(db, body.tenant_id, "evolve")

    from core_api.services.evolve_service import report_outcome

    # The service raises ValueError for its defensive validation gates
    # (outcome_type, outcome non-empty, scope, fleet_id coupling) so it
    # stays decoupled from FastAPI. Translate here so the REST contract
    # stays 422 for bad input; a 500 would be misleading when the body was
    # simply malformed in a way Pydantic couldn't catch.
    try:
        result = await report_outcome(
            db,
            tenant_id=body.tenant_id,
            outcome=body.outcome,
            outcome_type=body.outcome_type,
            related_ids=body.related_ids,
            scope=body.scope,
            agent_id=caller_agent_id,
            fleet_id=body.fleet_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    await log_action(
        db,
        tenant_id=body.tenant_id,
        action="evolve_report",
        resource_type="outcome",
        detail={
            "outcome_type": body.outcome_type,
            "scope": body.scope,
            "agent_id": caller_agent_id,
            "related_ids": body.related_ids,
        },
    )
    await db.commit()

    return result
