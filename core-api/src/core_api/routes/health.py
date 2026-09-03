import asyncio
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from common.events.factory import get_event_bus
from core_api import openapi_responses as _oar
from core_api.cache import redis_healthy
from core_api.clients.storage_client import get_storage_client
from core_api.config import settings
from core_api.constants import (
    HEALTH_PATH,
    PROBE_TIMEOUT_SECONDS,
    VERSION,
    VERSION_PATH,
)
from core_api.providers._platform import (
    get_platform_embedding,
    get_platform_init_errors,
    get_platform_llm,
)
from core_api.routes.plugin import _plugin_version
from core_api.services.agent_service import lookup_agent
from core_api.tools import REGISTRY  # SoT registry — populated at import time
from core_api.version_compat import MIN_RECOMMENDED_PLUGIN_VERSION

logger = logging.getLogger(__name__)

router = APIRouter(tags=["System"])


@router.get(VERSION_PATH, responses={200: {"model": _oar.VersionResponse}})
async def version():
    return {"version": VERSION}


def _gateway_verified(request: Request) -> bool:
    """True when the caller proved this request came through the gateway.

    Mirrors ``MCPAuthMiddleware``: the identity headers carry no credential
    of their own, so when a shared secret is configured the request must
    present it. When none is configured (OSS self-hosted, and the test
    suite) the header path is trusted exactly as before.
    """
    gw_secret = settings.gateway_shared_secret
    if not gw_secret:
        return True
    return hmac.compare_digest(request.headers.get("x-gateway-secret", ""), gw_secret)


async def _trust_fields(request: Request, tenant_id: str | None, agent_id: str | None) -> dict[str, Any]:
    """Resolve ``trust_level`` / ``trust_source`` for the identity probe.

    WHY THIS IS GUARDED, and it is the reason the field did not ship with
    ``key_kind`` (#1202): every other field on ``/whoami`` is an ECHO of what
    the caller sent, which is what makes an endpoint with no auth dependency
    survivable. ``trust_level`` cannot be an echo — it is a storage lookup
    keyed on caller-supplied ids. The perimeter check on the branch above
    closes the identity spoof only for deployments that CONFIGURE a gateway
    secret; with none set (OSS self-hosted) ``_gateway_verified`` trusts the
    header path by design. So an unguarded lookup here would let anyone on
    such a deployment read any agent's attributes in any tenant, by asking.

    The narrow answer: look up only when a gateway secret is configured AND
    THIS REQUEST presented it. Then the ids are the gateway's assertion rather
    than the caller's. Everywhere else the field degrades to ``None`` with a
    ``trust_source`` saying why, and the endpoint keeps looking nothing up —
    the property ``test_whoami_still_looks_nothing_up`` pins.

    Both halves of that check live HERE rather than being inherited from the
    caller. The sole call site is already inside ``whoami``'s verified branch,
    so the request-level check is redundant today — but a guard that is
    correct only because of where it is called is one refactor away from being
    wrong, and this one is the whole reason the lookup is allowed to exist.
    Self-contained, it stays correct if the call moves or a second caller
    appears.

    ``trust_source`` exists so ``trust_level: null`` is never ambiguous. A
    caller that cannot tell "no trust level applies to me" from "I could not
    find out" is back to discovering its permissions by attempting a
    destructive operation, which is the thing this field exists to stop.
    """
    # No agent identity ⇒ a tenant-scoped credential. Not "unknown": the trust
    # ladder governs agent credentials and does not apply here at all, so
    # ``None`` is the complete and correct answer (see ``enforce_delete``).
    if not agent_id or not tenant_id:
        return {"trust_level": None, "trust_source": "none"}
    if not settings.gateway_shared_secret or not _gateway_verified(request):
        return {"trust_level": None, "trust_source": "unavailable"}
    try:
        # Bounded like every other dependency call in this file. An error is
        # not the only way storage can ruin a probe: a backend that is UP BUT
        # SLOW never raises, and the storage client's read timeout is 120s
        # with retry-on-transient above it, so an unbounded await could hang
        # ``/whoami`` for minutes. ``PROBE_TIMEOUT_SECONDS`` exists for exactly
        # this — "a stalled backend can't hang the whole probe" — and this
        # field introduced the first I/O on this route, so it inherits the
        # rule rather than getting an exemption from it.
        #
        # ``asyncio.TimeoutError`` is an ``OSError`` subclass, so the handler
        # below already catches it; no separate clause needed.
        agent = await asyncio.wait_for(lookup_agent(tenant_id, agent_id), timeout=PROBE_TIMEOUT_SECONDS)
    except Exception:
        # This is the endpoint's only I/O, and it must not be able to take the
        # endpoint down. ``/whoami`` is a diagnostic probe that answered
        # without touching storage until this field existed; a caller reaches
        # for it precisely when something is already wrong, so returning 500
        # because storage is unreachable removes the tool at the moment it is
        # needed. Degrade to the same "could not determine" answer the
        # no-perimeter case gives — which is honest, and is exactly the
        # distinction ``trust_source`` exists to express: this is not a
        # statement about the caller's permissions, it is the absence of one.
        # Matches the storage/event-bus probes below, which degrade the same way.
        logger.exception("whoami trust lookup failed; degrading to unavailable")
        return {"trust_level": None, "trust_source": "unavailable"}
    if agent is None:
        # Registered-agent absence is a real, actionable answer: an
        # unregistered identity is refused by ``enforce_delete`` before any
        # trust comparison happens.
        return {"trust_level": None, "trust_source": "unregistered"}
    return {"trust_level": agent.get("trust_level", 0), "trust_source": "lookup"}


@router.get("/whoami")
async def whoami(request: Request) -> dict:
    """Identity probe — returns the caller's resolved (tenant_id, agent_id)
    along with the resolution source. A single round-trip answers ~80% of
    "is my integration wired correctly?" debugging during plugin / SDK
    bootstrap (friction §2.1, §2.8 / Stage 7).

    Resolution priority mirrors MCPAuthMiddleware:
      gateway-header — X-Tenant-ID (and optionally X-Agent-ID) injected
                       by the enterprise gateway after auth_request, and
                       only when the gateway secret verifies (below)
      standalone     — settings.is_standalone fixed tenant
      anonymous      — no auth resolved (caller will hit 401 on first
                       write; this endpoint stays open as a probe)

    ``trust_level`` / ``trust_source`` answer "may I delete?" without
    attempting a delete to find out. The two regimes differ and were not
    previously discoverable: an AGENT credential is governed by the trust
    ladder (``enforce_delete`` requires >= 3), while a TENANT key holds no
    trust level and is authorized by tenant scope instead. ``trust_source``
    distinguishes "no trust level applies to me" from "I could not find
    out" — see ``_trust_fields`` for why the lookup is guarded.
    """
    tenant_id = request.headers.get("x-tenant-id")
    agent_id = request.headers.get("x-agent-id")
    if tenant_id and not _gateway_verified(request):
        # Unverified identity headers make every field derived from them
        # unverified too — including ``via_gateway``, which unlike the fields
        # beside it is NOT an echo but core-api's own assertion about how the
        # request arrived. Claiming ``via_gateway: true`` on the strength of a
        # header the caller set themselves states as fact something never
        # checked, on the one endpoint whose whole job is telling an
        # integrator how their request actually resolves.
        #
        # Degrade rather than 401: a probe is most useful when it reports the
        # real resolution, and "your headers are not being trusted here" is
        # precisely the diagnosis a misconfigured integration needs. The
        # request falls through to standalone/anonymous below, which is what
        # a write on this same connection would resolve to.
        tenant_id = None
    if tenant_id:
        # Surface the cross-tenant scope the gateway plumbed so callers
        # can verify what their credential authorizes WITHOUT having to
        # probe each readable tenant one at a time. Single-tenant
        # credentials emit a readable list with just their home tenant.
        # ``capabilities`` exposes the credential's row-level scope so
        # SDKs can short-circuit obvious rejections (e.g. trying to
        # write with a read-only credential). ``auth_mode`` distinguishes
        # cross-tenant-key vs single-tenant for callers that care.
        readable_csv = request.headers.get("x-readable-tenant-ids", "") or ""
        readable = [t.strip() for t in readable_csv.split(",") if t.strip()]
        if not readable:
            readable = [tenant_id]
        caps_csv = request.headers.get("x-capabilities", "") or ""
        capabilities = [c.strip() for c in caps_csv.split(",") if c.strip()]
        return {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "auth_source": "gateway-header",
            "via_gateway": True,
            "readable_tenant_ids": readable,
            "capabilities": capabilities or None,
            "auth_mode": request.headers.get("x-auth-mode") or None,
            # Credential provenance the gateway already resolves (the same
            # header ``auth.py`` and the MCP middleware read). Saves a caller
            # guessing why an install-scoped key behaves differently from a
            # plain agent key.
            #
            # Echo, like ``capabilities`` and ``auth_mode`` beside it — this
            # endpoint reports what the gateway asserted about the caller and
            # looks it up only under the conditions ``_trust_fields`` states.
            "key_kind": (request.headers.get("x-caura-credential-kind") or "").lower() or None,
            **await _trust_fields(request, tenant_id, agent_id),
        }
    if settings.is_standalone:
        from core_api.standalone import get_standalone_tenant_id

        sid = get_standalone_tenant_id()
        return {
            "tenant_id": sid,
            "agent_id": None,
            "auth_source": "standalone",
            "via_gateway": False,
            "readable_tenant_ids": [sid] if sid else [],
            "capabilities": None,
            "auth_mode": None,
            "key_kind": None,
            # Standalone resolves a tenant and never an agent, so the trust
            # ladder does not apply — same answer as a tenant key.
            "trust_level": None,
            "trust_source": "none",
        }
    return {
        "tenant_id": None,
        "agent_id": None,
        "auth_source": "anonymous",
        "via_gateway": False,
        "readable_tenant_ids": [],
        "capabilities": None,
        "auth_mode": None,
        "key_kind": None,
        "trust_level": None,
        "trust_source": "none",
    }


@router.get(
    "/tool-descriptions",
    responses={200: {"model": dict[str, str] | dict[str, _oar.ToolDescriptionEnriched]}},
)
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


@router.get(HEALTH_PATH, responses={200: {"model": _oar.HealthResponse}})
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
