import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from common.events.factory import get_event_bus
from core_api.cache import redis_healthy
from core_api.clients.storage_client import get_storage_client
from core_api.config import settings
from core_api.constants import PROBE_TIMEOUT_SECONDS, VERSION
from core_api.providers._platform import (
    get_platform_embedding,
    get_platform_init_errors,
    get_platform_llm,
)
from core_api.routes.plugin import _plugin_version
from core_api.tools import REGISTRY  # SoT registry — populated at import time
from core_api.version_compat import MIN_RECOMMENDED_PLUGIN_VERSION

logger = logging.getLogger(__name__)

router = APIRouter(tags=["System"])


@router.get("/version")
async def version():
    return {"version": VERSION}


@router.get("/whoami")
async def whoami(request: Request) -> dict:
    """Identity probe — returns the caller's resolved (tenant_id, agent_id)
    along with the resolution source. A single round-trip answers ~80% of
    "is my integration wired correctly?" debugging during plugin / SDK
    bootstrap (friction §2.1, §2.8 / Stage 7).

    Resolution priority mirrors MCPAuthMiddleware:
      gateway-header — X-Tenant-ID (and optionally X-Agent-ID) injected
                       by the enterprise gateway after auth_request
      standalone     — settings.is_standalone fixed tenant
      anonymous      — no auth resolved (caller will hit 401 on first
                       write; this endpoint stays open as a probe)
    """
    tenant_id = request.headers.get("x-tenant-id")
    agent_id = request.headers.get("x-agent-id")
    if tenant_id:
        return {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "auth_source": "gateway-header",
            "via_gateway": True,
        }
    if settings.is_standalone:
        from core_api.standalone import get_standalone_tenant_id

        return {
            "tenant_id": get_standalone_tenant_id(),
            "agent_id": None,
            "auth_source": "standalone",
            "via_gateway": False,
        }
    return {
        "tenant_id": None,
        "agent_id": None,
        "auth_source": "anonymous",
        "via_gateway": False,
    }


@router.get("/tool-descriptions")
async def tool_descriptions(enriched: bool = False):
    """Return tool descriptions, derived from the SoT registry.

    Default: ``{name: description}`` (backward compatible).
    With ``?enriched=true``: ``{name: {description, stm_only}}``.

    The registry is the single source of truth — ``stm_only`` is
    derived from ``spec.plugin_exposed`` (inverted).
    """
    if enriched:
        return {
            spec.name: {
                "description": spec.description,
                "stm_only": not spec.plugin_exposed,
            }
            for spec in REGISTRY.values()
        }
    return {spec.name: spec.description for spec in REGISTRY.values()}


async def _probe_dependencies() -> tuple[dict[str, Any], list[str]]:
    """Probe storage / redis / event_bus and return ``(result, unhealthy)``.

    Shared between ``GET /health`` (which 503s on any unhealthy dep) and
    ``GET /status`` (which surfaces the same shape but never fails the
    response code on it). Each probe is bounded by ``PROBE_TIMEOUT_SECONDS``
    so a stalled backend can't hang the whole call.
    """
    result: dict[str, Any] = {}
    unhealthy: list[str] = []

    try:
        sc = get_storage_client()
        await asyncio.wait_for(
            sc.count_all(tenant_id="__health_check__"),
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        result["storage"] = "connected"
    except Exception:
        # Never surface str(exc) — httpx errors embed the target URL
        # (and any basic-auth creds in it) into the response body.
        logger.exception("Storage health check failed")
        result["storage"] = "unreachable"
        unhealthy.append("storage")

    if settings.redis_url:
        try:
            redis_ok = await asyncio.wait_for(redis_healthy(), timeout=PROBE_TIMEOUT_SECONDS)
        except Exception:
            redis_ok = False
        if redis_ok:
            result["redis"] = "connected"
        else:
            result["redis"] = "unavailable"
            unhealthy.append("redis")
    else:
        result["redis"] = "not configured"

    # Event bus: ``is_healthy`` is sync (no I/O), no timeout wrapper.
    # ``get_event_bus()`` itself can raise (Pub/Sub env vars missing,
    # unknown backend); wrap consistently so those surface as 503-shaped
    # output rather than a bare 500.
    try:
        bus = get_event_bus()
        if bus.is_healthy:
            result["event_bus"] = "ok"
        else:
            result["event_bus"] = "unhealthy"
            unhealthy.append("event_bus")
    except Exception:
        logger.exception("Event bus health check failed")
        result["event_bus"] = "error"
        unhealthy.append("event_bus")

    return result, unhealthy


@router.get("/health")
async def health(response: Response):
    """Liveness + readiness probe.

    Returns 503 when any required dependency is unavailable so deploy
    gates and Cloud Run health checks can fail-fast on status code alone.
    Required deps:
      - storage (core-storage-api): always
      - redis:                      only when ``settings.redis_url`` is set
                                    (empty url = in-memory fallback, OSS default)
      - event_bus:                  ``InProcessEventBus`` always reports ok;
                                    ``PubSubEventBus`` reports ``unhealthy``
                                    when a pull loop has halted on a permanent
                                    subscription / IAM error.

    Non-critical issues (platform provider init errors) flip status to
    ``"degraded"`` but keep a 200 — the app can still serve requests.
    """
    deps, unhealthy = await _probe_dependencies()
    result: dict[str, Any] = {"status": "ok", **deps}

    init_errors = get_platform_init_errors()
    if init_errors:
        if not unhealthy:
            # 200/degraded path — safe to surface detail since operators
            # need actionable info when everything else is fine.
            # Same key name as ``/status`` (see status_ below): both
            # surfaces call ``get_platform_init_errors`` and should
            # expose the result under one canonical name so dashboards
            # / clients reading either endpoint see the same shape.
            result["platform_init_errors"] = init_errors
            result["status"] = "degraded"
        else:
            # 503 path — don't leak internal SDK messages (hostnames,
            # service URLs) alongside a deploy-gate-visible response.
            logger.warning("Platform init errors alongside dep failures: %s", init_errors)

    if unhealthy:
        result["status"] = "unhealthy"
        result["unhealthy_dependencies"] = unhealthy
        response.status_code = HTTP_503_SERVICE_UNAVAILABLE

    return result


@router.get("/status")
async def status_() -> dict[str, Any]:
    """Public service-fingerprint endpoint — version, mode, providers, deps.

    Distinct from ``/health`` (which returns 503 to fail deploy gates) and
    ``/stats`` (which returns row counts). ``/status`` describes the *shape*
    of the running service: which models are loaded, which dependencies
    answer, what version is deployed.

    Provider names and model identifiers are intentional public knowledge
    (already in marketing copy and the FAQ). Secrets — API keys, GCP
    project IDs / locations, internal hostnames, raw SDK error strings —
    are NEVER surfaced; ``platform_init_errors`` reports the symbolic tag
    set populated by ``init_platform_providers`` (e.g. ``"vertex-llm-config"``,
    ``"openai-embedding"``), never the underlying message.
    """
    deps, unhealthy = await _probe_dependencies()
    init_errors = get_platform_init_errors()

    # The status field carries the rolled-up health enum, NOT a duration
    # — name it ``health`` rather than ``uptime`` so dashboards reading
    # the value programmatically don't misinterpret it.
    if unhealthy:
        health_state = "unhealthy"
    elif init_errors:
        health_state = "degraded"
    else:
        health_state = "ok"

    llm = get_platform_llm()
    emb = get_platform_embedding()

    # ``mode`` (oss vs enterprise) is intentionally NOT surfaced on this
    # public unauthenticated endpoint: it would directly signal whether
    # ``settings.is_standalone`` (the tenant-auth-bypass opt-in) is
    # active, telling an unauthenticated probe what the auth model is
    # before they've shown any credentials. The OSS/enterprise split is
    # discoverable from a service's image tag and config in operator
    # contexts that already have access; we don't owe it to anonymous
    # callers.
    return {
        "version": VERSION,
        "plugin_version": _plugin_version(),
        "plugin_min_recommended": MIN_RECOMMENDED_PLUGIN_VERSION,
        "health": health_state,
        "dependencies": deps,
        "llm": {
            "provider": getattr(llm, "provider_name", None),
            "model": getattr(llm, "model", None),
            "configured": llm is not None,
        },
        "embedding": {
            "provider": getattr(emb, "provider_name", None),
            "model": getattr(emb, "model", None),
            "configured": emb is not None,
        },
        # Symbolic tags only (see docstring above) — safe to expose.
        "platform_init_errors": init_errors,
    }
