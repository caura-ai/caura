"""Mount inside the real Caura core-api, retaining its auth and middleware."""

import hmac
from typing import Annotated

import httpx
from caura_bus_platform.collaboration_routes import HumanPrincipal, human_router
from caura_bus_platform.routes import Operation, Principal, public_router
from fastapi import Depends, HTTPException, Request

from core_api.app import app
from core_api.auth import AuthContext, get_auth_context
from core_api.clients.storage_client import get_storage_client
from core_api.config import settings


async def bus_principal(request: Request, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    # Bus requires enterprise agent credentials even if OSS anonymous mode is enabled.
    secret = settings.gateway_shared_secret
    if not secret or not hmac.compare_digest(request.headers.get("x-gateway-secret", ""), secret):
        raise HTTPException(401, "Caura gateway authentication is required")
    if (
        request.headers.get("x-caura-credential-kind") != "agent_key"
        or not auth.tenant_id
        or not auth.agent_id
    ):
        raise HTTPException(403, "use a Caura agent-scoped credential")
    if not auth.capabilities or "read" not in auth.capabilities:
        raise HTTPException(403, "read capability required")
    if request.method != "GET":
        auth.enforce_read_only()
        auth.enforce_usage_limits()
    return Principal(tenant_id=auth.tenant_id, agent_id=str(auth.agent_id))


async def human_principal(request: Request, auth: Annotated[AuthContext, Depends(get_auth_context)]):
    secret = settings.gateway_shared_secret
    if not secret or not hmac.compare_digest(request.headers.get("x-gateway-secret", ""), secret):
        raise HTTPException(401, "Caura gateway authentication is required")
    # Auth service emits user and role only for human sessions/JWTs; the
    # verified gateway overwrites them. API keys cannot decide for a human.
    user = request.headers.get("x-user-id")
    if not user or not auth.tenant_id or auth.agent_id or auth.org_role not in {"owner", "admin", "member"}:
        raise HTTPException(403, "a signed-in Caura human is required")
    if request.method != "GET":
        auth.enforce_read_only()
    return HumanPrincipal(
        tenant_id=auth.tenant_id,
        user_id=user,
        is_admin=auth.org_role in {"owner", "admin"},
        role=auth.org_role,
    )


async def storage_call(operation):
    try:
        return await get_storage_client()._post(
            "/bus/execute",
            operation.model_dump(),
            idempotent=operation.operation
            in {
                "send",
                "ack",
                "identity",
                "agents",
                "recent",
                "threads",
                "status",
                "memory_context",
                "signals",
                "inbox_state",
                "discover",
                "human_signals",
                "human_inbox",
                "human_case",
                "human_discover",
                "human_send",
                "human_recent",
                "human_work",
                "human_decide",
                "human_read",
            },
        )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in {403, 404, 409, 422}:
            raise HTTPException(status, exc.response.json().get("detail", "request rejected")) from exc
        raise HTTPException(503, "Caura storage is unavailable") from exc
    except httpx.TransportError as exc:
        raise HTTPException(503, "Caura storage is unavailable") from exc


app.include_router(public_router(bus_principal, storage_call))
app.include_router(human_router(human_principal, storage_call, Operation))
# Upstream builds its schema during import to check route invariants.
app.openapi_schema = None
