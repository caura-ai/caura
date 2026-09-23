"""Dedicated collaboration workload using the existing core image and identity boundary."""

import asyncio
import hmac
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from caura_bus_platform.collaboration_routes import HumanPrincipal, human_router
from caura_bus_platform.liveness import SuppressionCache
from caura_bus_platform.routes import Operation, Principal, public_router
from caura_bus_platform.runtime import (
    AdmissionMiddleware,
    Runtime,
    send_deadline,
    shutdown_signals,
    stop_task,
)
from caura_bus_platform.settings import settings as collaboration_settings
from caura_bus_platform.timing import TimingMiddleware, span
from caura_bus_platform.wake import WakeHub
from fastapi import Depends, FastAPI, HTTPException, Request, Security

from core_api.app import app as memory_app
from core_api.auth import AuthContext, api_key_header, get_auth_context
from core_api.bus_storage import close_storage_client, get_presence_storage_client, get_storage_client
from core_api.config import settings
from core_api.middleware.request_timeout import RequestTimeoutMiddleware
from core_api.suppression import use_suppression_lookup


async def _suppression_lookup(tenant):
    return await get_presence_storage_client().is_tenant_suppressed(tenant)


suppression_cache = SuppressionCache(_suppression_lookup)


async def measured_auth_context(request: Request, key: str | None = Security(api_key_header)):
    # Suppression is a tenant-wide property shared by all validated principals.
    liveness = request.method == "PUT" and request.url.path == "/api/v1/bus/presence"

    async def lookup(tenant):
        with span("suppression_wait"):
            return await suppression_cache.check(tenant, liveness=liveness)

    with span("credential_auth"), use_suppression_lookup(lookup):
        return await get_auth_context(request, key)


async def bus_principal(request: Request, auth: Annotated[AuthContext, Depends(measured_auth_context)]):
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


async def human_principal(request: Request, auth: Annotated[AuthContext, Depends(measured_auth_context)]):
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


# Reconstruct only this fixed operator vocabulary; storage text stays private.
DECISION_CONFLICTS = {
    "REQUEST_NOT_DECIDABLE": "This decision is no longer current. Refresh before trying again.",
    "TARGET_UNAVAILABLE": "Choose an online agent in this tenant.",
    "TARGET_ALREADY_ASSIGNED": "This agent already has this request. Choose another agent.",
    "STOP_NOT_CONFIRMED": "Stop is not confirmed. Wait for confirmation or acknowledge unconfirmed recovery.",
}


async def storage_call(operation):
    if operation.operation in {"send", "human_send", "reply"}:
        operation = operation.model_copy(update={"deadline_at": send_deadline.get()})
    with span("storage_rpc"):
        return await _storage_call(operation)


async def _storage_call(operation):
    try:
        client = get_presence_storage_client() if operation.operation == "presence" else get_storage_client()
        return await client._post(
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
        # Never forward storage exception text, paths or arbitrary payload fields.
        if status == 503 and exc.response.headers.get("x-caura-send-result") == "deadline":
            runtime.cancelled_sends += 1
            raise HTTPException(
                503,
                "Collaboration send deadline exceeded",
                headers={"Retry-After": "1", "X-Caura-Send-Result": "deadline"},
            ) from exc
        if status == 409:
            try:
                payload = exc.response.json()
            except ValueError:
                payload = None  # A malformed error body still maps to a fixed conflict.
            detail = payload.get("detail") if isinstance(payload, dict) else None
            code = detail.get("code") if isinstance(detail, dict) else None
            if operation.operation == "human_decide" and isinstance(code, str) and code in DECISION_CONFLICTS:
                raise HTTPException(409, {"code": code, "message": DECISION_CONFLICTS[code]}) from exc
            if isinstance(detail, dict) and detail.get("state") == "paused":
                raise HTTPException(
                    409, {"state": "paused", "detail": "Delivery paused; call wait for current context"}
                ) from exc
        public_errors = {
            403: "Caura operation is forbidden",
            404: "Caura resource was not found",
            409: "Caura operation conflicts with the current state",
            422: "Caura request is invalid",
        }
        if status in public_errors:
            raise HTTPException(status, public_errors[status]) from exc
        raise HTTPException(503, "Caura storage is unavailable", headers={"Retry-After": "1"}) from exc
    except httpx.TransportError as exc:
        raise HTTPException(503, "Caura storage is unavailable", headers={"Retry-After": "1"}) from exc


wake_hub = WakeHub()
runtime = Runtime(wake_hub, get_storage_client, get_presence_storage_client)


@asynccontextmanager
async def lifespan(app):
    from common.events.factory import get_event_bus

    get_storage_client()
    runtime.bus = get_event_bus()
    wake_hub.draining = False
    wake_hub.register(runtime.bus)
    await runtime.bus.start()
    metrics_task = asyncio.create_task(runtime.refresh_metrics())
    try:
        with shutdown_signals(wake_hub):
            yield
    finally:
        wake_hub.drain()
        await stop_task(metrics_task)
        try:
            await runtime.bus.stop()
        finally:
            await suppression_cache.close()
            await close_storage_client()


# Reuse the canonical error envelope and security headers without starting
# memory providers, memory background work, or mounting its API routes.
app = FastAPI(
    title="Caura collaboration",
    lifespan=lifespan,
    middleware=[
        m
        for m in memory_app.user_middleware
        if getattr(m.cls, "__name__", None) in {"SecurityHeadersMiddleware", "CORSMiddleware"}
    ],
)
app.exception_handlers.update(memory_app.exception_handlers)
app.add_middleware(RequestTimeoutMiddleware, timeout_seconds=collaboration_settings.request_timeout_seconds)
app.add_middleware(AdmissionMiddleware, runtime=runtime)
app.add_middleware(TimingMiddleware, service="collaboration-api")


runtime.install(app)
app.include_router(public_router(bus_principal, storage_call, wake_hub))
app.include_router(human_router(human_principal, storage_call, Operation, wake_hub))
