"""Corrections: who will be told (CP-10), sending the notices (CP-11), and who confirmed (CP-15).

When a person corrects a memory, core-api publishes ``caura.memory.corrected``
and its consumer records a ``memory_correction`` with the agents that read the
memory before the correction (``memory_exposure.first_seen < corrected_at``),
the correcting agent excluded, at most 50. These routes read that record back,
before anything is sent: the person's preview.

Tenant-scoped, and the same access rule as ``GET /memories/{id}`` on the
corrected memory: another tenant's correction, or one on a memory the caller
cannot read, is a 404. An agent that is not a recipient does not see
``old_summary`` (``services.correction_recipients.redact_for_caller``).

``POST /corrections/{id}/send`` sends the notices (``services.correction_sender``):
a person, an admin or tenant key, or an agent at trust_level >= 3. A status
read refreshes the bus outcomes. ``POST /corrections/{id}/confirm`` is the
recipient agent's own confirmation.

See ``docs/plans/correction-propagation.md``.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from core_api import openapi_responses as _oar
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.errors import AUTH_AGENT_CREDENTIAL_FORBIDDEN, AUTH_AGENT_IDENTITY_MISMATCH, coded_detail
from core_api.services.agent_service import authorize_memory_access, enforce_send_correction
from core_api.services.correction_recipients import redact_for_caller
from core_api.services.correction_sender import confirm_as_agent, refresh_correction, send_correction
from core_api.services.verification import verifier_id

router = APIRouter(tags=["Corrections"])


async def _readable_memory(memory_id: str, tenant_id: str, auth: AuthContext) -> bool:
    memory = await get_storage_client().get_memory(memory_id, tenant_id, include_held=auth.is_person)
    return memory is not None and await authorize_memory_access(
        tenant_id,
        auth.agent_id,
        visibility=memory.get("visibility"),
        owner_agent_id=memory.get("agent_id"),
        fleet_id=memory.get("fleet_id"),
        caller_tenant_id=auth.tenant_id,
        is_install_credential=auth.is_install_credential,
        install_uuid=auth.install_uuid,
    )


@router.get("/corrections/{correction_id}", responses={200: {"model": _oar.MemoryCorrectionOut}})
async def get_correction(
    correction_id: UUID,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
):
    """One correction and the agents it will tell: the preview before sending.

    ``recipients`` are the agents that read the memory before it was corrected,
    latest read first, at most 50; ``counts.truncated`` are the readers past
    the cap. 404 for another tenant's correction or a memory the caller cannot
    read.
    """
    auth.enforce_readable_tenant(tenant_id)
    correction = await get_storage_client().get_memory_correction(tenant_id, str(correction_id))
    if correction is None or not await _readable_memory(correction["memory_id"], tenant_id, auth):
        raise HTTPException(status_code=404, detail="Correction not found")
    correction = await refresh_correction(tenant_id, correction)
    return redact_for_caller(correction, agent_id=auth.agent_id, is_person=auth.is_person)


@router.post("/corrections/{correction_id}/send", responses={200: {"model": _oar.MemoryCorrectionOut}})
async def send_correction_notices(
    correction_id: UUID,
    tenant_id: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
):
    """Send the correction's notices to the agents that read the old version.

    Each recipient gets a Caura bus notice; an agent the bus does not know, or
    every agent when no bus is configured, is told on its next recall instead
    (``channel: recall``). Moves the correction ``preview`` → ``sending`` →
    ``sent``. Idempotent: a second call returns the record, retrying only
    recipients the bus throttled. Open to a signed-in person, an admin or
    tenant key, and an agent at ``trust_level >= 3`` that can read the memory.
    """
    auth.enforce_read_only()
    auth.enforce_tenant(tenant_id)
    auth.enforce_tenant_admin_credential("send correction notices")
    if auth.agent_id:
        await enforce_send_correction(tenant_id, auth.agent_id)
    correction = await get_storage_client().get_memory_correction(tenant_id, str(correction_id))
    if correction is None or not await _readable_memory(correction["memory_id"], tenant_id, auth):
        raise HTTPException(status_code=404, detail="Correction not found")
    sent = await send_correction(
        tenant_id,
        str(correction_id),
        sent_by=verifier_id(agent_id=auth.agent_id, user_id=auth.user_id, credential_kind=auth.actor_kind),
    )
    if sent is None:
        raise HTTPException(status_code=404, detail="Correction not found")
    return sent


@router.post(
    "/corrections/{correction_id}/confirm", responses={200: {"model": _oar.CorrectionConfirmResponse}}
)
async def confirm_correction(
    correction_id: UUID,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(
        default=None,
        description="The confirming agent, for a credential that names none (a tenant-key plugin).",
    ),
    auth: AuthContext = Depends(get_auth_context),
):
    """The recipient agent confirms it has seen the correction and rechecked its work.

    The caller must be an agent on the correction's recipient list (404
    otherwise). Idempotent: a second confirm keeps the first ``confirmed_at``.
    """
    auth.enforce_read_only()
    auth.enforce_tenant(tenant_id)
    agent = auth.effective_agent_id(agent_id)
    if auth.is_person or not agent:
        raise HTTPException(
            status_code=403,
            detail=coded_detail(
                AUTH_AGENT_CREDENTIAL_FORBIDDEN if auth.is_person else AUTH_AGENT_IDENTITY_MISMATCH,
                "Only the recipient agent can confirm a correction notice; name it with agent_id "
                "when the credential names no agent.",
            ),
        )
    return await confirm_as_agent(tenant_id, str(correction_id), str(agent))


@router.get(
    "/memories/{memory_id}/corrections",
    responses={200: {"model": _oar.MemoryCorrectionListResponse}},
)
async def list_memory_corrections(
    memory_id: UUID,
    tenant_id: str = Query(...),
    limit: int = Query(default=20, ge=1, le=100),
    auth: AuthContext = Depends(get_auth_context),
):
    """The memory's corrections, newest first, each with the agents it will tell.

    Same access rule as ``GET /memories/{id}``; a memory the caller cannot read
    is a 404.
    """
    auth.enforce_readable_tenant(tenant_id)
    if not await _readable_memory(str(memory_id), tenant_id, auth):
        raise HTTPException(status_code=404, detail="Memory not found")
    corrections = await get_storage_client().list_memory_corrections(tenant_id, str(memory_id), limit=limit)
    return {
        "memory_id": str(memory_id),
        "corrections": [
            redact_for_caller(c, agent_id=auth.agent_id, is_person=auth.is_person) for c in corrections
        ],
    }
