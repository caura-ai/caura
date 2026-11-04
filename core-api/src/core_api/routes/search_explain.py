"""``POST /api/v1/search/explain``: why did memory X not come back? (EC-06)

"It forgot" is the most frequent memory complaint, and answering it used to take
database access and about an hour: the ``diagnostic`` search mode only shows
memories that reached scoring, not the filters that removed one before it.

This route runs the search the named agent would run for the query — same
identity resolution, fleet forcing, search profile and tenant config as
``POST /search`` — through the explain composition, and names the FIRST stage
that removed the memory (see ``core_api.services.recall_explain``).

Access: a tenant-admin credential (the admin plane's rule) or an agent credential
at trust level 3 or more. The answer confirms that a memory exists and says who wrote it and where
it lives, which is more than a search returns.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from core_api.agent_ids import canonical_service_agent_id
from core_api.auth import AuthContext, get_auth_context
from core_api.errors import AUTH_AGENT_TRUST_TOO_LOW, coded_detail
from core_api.middleware.per_tenant_concurrency import per_tenant_slot
from core_api.middleware.rate_limit import search_limit
from core_api.routes.memories import _request_ranking_knobs, _resolve_read_identity
from core_api.schemas import SearchRequest
from core_api.services.agent_service import lookup_agent
from core_api.services.recall_explain import classify_explanation

router = APIRouter(tags=["Memories"])

_ACTION = "explain a search"


class SearchExplainRequest(SearchRequest):
    """A ``POST /search`` body, plus the memory to explain and who searches."""

    memory_id: UUID = Field(description="The memory that was expected and did not come back.")
    as_agent_id: str | None = Field(
        default=None,
        description=(
            "Explain the search as this agent would run it (its visibility, fleet scope and "
            "search profile). Defaults to the identity /search would resolve for this body."
        ),
    )


class SearchExplainCheck(BaseModel):
    filter: str
    passed: bool


class SearchExplainResponse(BaseModel):
    memory_id: str
    returned: bool = Field(description="True when the search returns the memory.")
    stage: str | None = Field(description="filter | entity_lookup | ranking | returned")
    removed_by: str | None = Field(
        description=(
            "The first step that removed the memory: a filter name (tenant_scope, deleted, held, "
            "embedding_missing, fleet_scope, visibility_scope, agent_filter, memory_type_filter, verified_only, "
            "status_filter, status_superseded, temporal_valid_at), not_found, "
            "entity_lookup_short_circuit, below_min_similarity, derived_excluded or beyond_top_k. "
            "Null when returned."
        )
    )
    reason: str | None
    rank: int | None = Field(
        default=None, description="1-based position among scored candidates, or in the result when returned."
    )
    top_k: int | None = None
    retrieval_strategy: str | None = None
    searched_as_agent_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    checks: list[SearchExplainCheck] = Field(
        default_factory=list, description="Every search filter, in order, and whether this memory passed it."
    )
    memory: dict[str, Any] | None = Field(
        default=None, description="Where the memory lives (tenant, fleet, author, visibility, status)."
    )


async def _require_explain_access(auth: AuthContext, tenant_id: str) -> None:
    """An agent credential at trust level >= 3, or a tenant-admin credential.

    "Tenant admin" is the admin plane's rule (settings, agent trust): the admin
    key, a tenant API key, standalone, an org admin's session. Refused there and
    here: org members, install credentials and cross-tenant credentials.
    """
    if auth.is_admin:
        return
    if auth.agent_id:
        agent = await lookup_agent(auth.tenant_id or tenant_id, auth.agent_id)
        if agent is None or agent.get("trust_level", 0) < 3:
            raise HTTPException(
                status_code=403,
                detail=coded_detail(
                    AUTH_AGENT_TRUST_TOO_LOW,
                    f"Agent '{auth.agent_id}' needs trust level 3 to explain a search: the answer "
                    "reveals memories the agent may not be able to read.",
                ),
            )
        return
    auth.enforce_not_org_member(_ACTION)
    auth.enforce_tenant_admin_credential(_ACTION)


@router.post("/search/explain", response_model=SearchExplainResponse)
@search_limit
async def search_explain(
    request: Request,
    body: SearchExplainRequest,
    response: Response,
    auth: AuthContext = Depends(get_auth_context),
) -> SearchExplainResponse:
    auth.enforce_readable_tenant(body.tenant_id)
    await _require_explain_access(auth, body.tenant_id)

    if body.as_agent_id:
        eff_agent_id: str | None = canonical_service_agent_id(body.as_agent_id)
    else:
        eff_agent_id, _ = _resolve_read_identity(auth, body)

    # The agent's row, for the fleet forcing and search profile /search applies.
    # Looked up, never created: an explanation must not register an agent.
    agent = await lookup_agent(body.tenant_id, eff_agent_id) if eff_agent_id else None
    fleet_ids = body.fleet_ids
    if not fleet_ids and agent and agent.get("fleet_id") and agent.get("trust_level", 0) < 2:
        fleet_ids = [agent["fleet_id"]]  # /search forces fleet scoping for trust < 2

    from core_api.services.memory_service import _search_memories_pipeline
    from core_api.services.organization_settings import resolve_config

    async with per_tenant_slot("search", body.tenant_id):
        config = await resolve_config(body.tenant_id)
        explain_ctx: dict = {"memory_id": body.memory_id}
        await _search_memories_pipeline(
            body.tenant_id,
            body.query,
            fleet_ids=fleet_ids,
            filter_agent_id=body.filter_agent_id,
            caller_agent_id=eff_agent_id,
            # The searching agent's home tenant: an agent's own scope_agent rows
            # live there. /search passes the credential's tenant, which is the
            # same tenant for an agent credential.
            caller_tenant_id=auth.tenant_id or body.tenant_id,
            # An explanation is not a recall: never bump recall_count.
            **_request_ranking_knobs(body, config, allow_recall_bump=False),
            memory_type_filter=body.memory_type_filter,
            status_filter=body.status_filter,
            valid_at=body.valid_at,
            top_k=body.top_k,
            graph_expand=config.graph_expand,
            tenant_config=config,
            search_profile=agent.get("search_profile") if agent else None,
            readable_tenant_ids=auth.readable_tenant_ids if auth.is_cross_tenant_read else None,
            source="explain",
            min_similarity=body.min_similarity,
            include_derived=body.include_derived,
            top_k_explicit="top_k" in body.model_fields_set,
            # EC-11 — the same filter /search applies, so a verified-only
            # search is explained as the search it is.
            verified_only=body.verified_only,
            explain_ctx=explain_ctx,
        )

    out = classify_explanation(explain_ctx["data"], body.memory_id, reveal_cross_tenant=auth.is_admin)
    return SearchExplainResponse(searched_as_agent_id=eff_agent_id, **out)
