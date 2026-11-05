"""Handoff batons for a receiver that is not on the bus: read one, accept it (HB-09).

Handoff batons (``docs/plans/handoff-batons.md``), D4. The bus delivers a baton
and serves it (``GET /api/v1/bus/batons/{id}``) to agents on the bus. An agent
that only recalls sees a ``baton_notices`` entry on its next recall
(``services.baton_notices``) and uses these two routes:

- ``GET /batons/{id}``: the baton as this receiver got it (its own delivered
  links, each re-checked against its read access today, with titles), or as
  its sender cited it. Anyone else gets the same 404 as for an unknown baton.
- ``POST /batons/{id}/accept``: the receiver takes it up, so the notice stops;
  with a bus configured, its delivery is acked there too.

Identity is the correction-confirm rule: an agent credential acts as itself; a
tenant key names the agent with ``agent_id``; a signed-in person cannot.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from core_api import openapi_responses as _oar
from core_api.auth import AuthContext, get_auth_context
from core_api.errors import AUTH_AGENT_CREDENTIAL_FORBIDDEN, AUTH_AGENT_IDENTITY_MISMATCH, coded_detail
from core_api.services.baton_notices import accept_as_agent, read_as_agent

router = APIRouter(tags=["Handoff batons"])


def _receiver(auth: AuthContext, agent_id: str | None, action: str) -> str:
    agent = auth.effective_agent_id(agent_id)
    if auth.is_person or not agent:
        raise HTTPException(
            status_code=403,
            detail=coded_detail(
                AUTH_AGENT_CREDENTIAL_FORBIDDEN if auth.is_person else AUTH_AGENT_IDENTITY_MISMATCH,
                f"Only the baton's agents can {action} it; name the agent with agent_id "
                "when the credential names none.",
            ),
        )
    return str(agent)


@router.get("/batons/{baton_id}", responses={200: {"model": _oar.BatonOut}})
async def get_baton(
    baton_id: UUID,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(
        default=None, description="The reading agent, for a credential that names none (a tenant-key plugin)."
    ),
    auth: AuthContext = Depends(get_auth_context),
):
    """The baton as this agent got it (receiver) or cited it (sender); 404 for anyone else."""
    auth.enforce_tenant(tenant_id)
    agent = _receiver(auth, agent_id, "read")
    return await read_as_agent(tenant_id, str(baton_id), agent)


@router.post("/batons/{baton_id}/accept", responses={200: {"model": _oar.BatonAcceptResponse}})
async def accept_baton(
    baton_id: UUID,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(
        default=None,
        description="The accepting agent, for a credential that names none (a tenant-key plugin).",
    ),
    auth: AuthContext = Depends(get_auth_context),
):
    """The receiver takes the baton up: its recall notice stops.

    The caller must be a receiver of the baton (404 otherwise). Idempotent: a
    second accept keeps the first ``accepted_at``.
    """
    auth.enforce_read_only()
    auth.enforce_tenant(tenant_id)
    agent = _receiver(auth, agent_id, "accept")
    return await accept_as_agent(tenant_id, str(baton_id), agent)
