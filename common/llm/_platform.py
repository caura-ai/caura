"""Platform-default LLM-provider singleton — moved from
``core_api.providers._platform`` (CAURA-595).

Pre-built at startup from ``PLATFORM_LLM_*`` env vars. Returned by the
LLM registry when a tenant has no credentials configured — tier 2 in
the three-tier resolution:

    Tenant key  →  Platform singleton  →  FakeLLMProvider

Security: keys are sealed into the singleton objects at construction
time and never enter tenant-configurable code paths
(``resolve_openai_compatible``, ``ResolvedConfig``, etc.).

Settings reads here are env-driven (``os.environ``) so the worker can
init the same singleton without depending on ``core_api.config``.
"""

from __future__ import annotations

import logging
import os
import threading

from common.embedding import (
    get_platform_embedding,
)
from common.embedding import (
    get_platform_init_errors as _get_embedding_init_errors,
)
from common.embedding import (
    init_platform_embedding as _init_platform_embedding,
)
from common.llm.constants import (
    LLM_FALLBACK_MODEL_OPENAI,
    OPENAI_CHAT_BASE_URL,
    VERTEX_LLM_DEFAULT_MODEL,
)
from common.llm.protocols import LLMProvider
from common.provider_names import ProviderName

logger = logging.getLogger(__name__)

_platform_llm: LLMProvider | None = None
# Handle for the background client warm-up (see _start_warmup_thread).
# Module-level so tests can join it for deterministic assertions.
_warmup_thread: threading.Thread | None = None


def _start_warmup_thread(provider: LLMProvider) -> None:
    """Kick off the provider's client warm-up on a daemon thread.

    Never raises: a warm-up failure is logged and the provider falls
    back to building its client lazily on first call (the pre-warm-up
    behavior). Daemon so a hung credential fetch can never block
    process shutdown.
    """
    global _warmup_thread

    # Duck-typed: ``warm_up`` is provider-specific (not part of the
    # ``LLMProvider`` Protocol) — providers without one need no warming.
    warm_up = getattr(provider, "warm_up", None)
    if warm_up is None:
        return

    def _warm() -> None:
        try:
            warm_up()
        except Exception:
            logger.warning(
                "Platform Vertex client warm-up failed; the client "
                "will be built lazily on first call instead",
                exc_info=True,
            )

    _warmup_thread = threading.Thread(
        target=_warm, name="platform-llm-client-warmup", daemon=True
    )
    _warmup_thread.start()
_platform_init_errors: list[str] = []

__all__ = [
    "get_platform_embedding",
    "get_platform_init_errors",
    "get_platform_llm",
    "init_platform_providers",
]


def init_platform_providers() -> None:
    """Build singleton provider instances from PLATFORM_* env vars.

    Initialises BOTH the LLM and embedding singletons. Called once
    during app lifespan startup.
    """
    global _platform_llm

    _platform_llm = None
    _platform_init_errors.clear()

    provider = os.environ.get("PLATFORM_LLM_PROVIDER", "")
    project_id = os.environ.get("PLATFORM_LLM_GCP_PROJECT_ID", "")
    location = os.environ.get("PLATFORM_LLM_GCP_LOCATION", "")
    model_override = os.environ.get("PLATFORM_LLM_MODEL", "")
    api_key = os.environ.get("PLATFORM_LLM_API_KEY", "")

    # ── LLM ──────────────────────────────────────────────────────────
    if provider == ProviderName.VERTEX:
        if project_id:
            try:
                from common.llm.providers.vertex import VertexLLMProvider

                resolved_location = location or "us-central1"
                resolved_model = model_override or VERTEX_LLM_DEFAULT_MODEL
                _platform_llm = VertexLLMProvider(
                    project_id=project_id,
                    location=resolved_location,
                    model=resolved_model,
                )
                logger.info(
                    "Platform LLM: vertex/%s (%s/%s)",
                    resolved_model,
                    project_id,
                    resolved_location,
                )
                # Warm the genai client in the BACKGROUND at startup
                # instead of letting the first request pay for it: the
                # lazy build (SDK import + ADC discovery + HTTP pool)
                # measured ~13s per cold instance on staging
                # (2026-09-01) — enough to trip recall's 15s
                # per-attempt cap once per instance.
                #
                # A daemon thread rather than an inline (or awaited
                # to_thread) call, deliberately: this function runs
                # inside the services' async lifespans, so an inline
                # build would block the event loop, and awaiting it
                # would add the full ~13s to readiness — i.e. to every
                # Cloud Run scale-up cold start. The thread does
                # neither. The race is safe: a request that lands
                # before warm-up completes blocks on the provider's
                # ``_client_lock`` inside its own to_thread worker —
                # the pre-warm-up first-call behavior, at worst once.
                # Non-fatal by design: environments without ADC
                # (tests, misconfigured installs) log a warning and
                # fall back to the lazy build on first call.
                _start_warmup_thread(_platform_llm)
            except Exception:
                logger.exception("Failed to initialize platform Vertex LLM provider")
                _platform_init_errors.append("vertex-llm")
        else:
            logger.warning(
                "PLATFORM_LLM_PROVIDER=vertex but no PLATFORM_LLM_GCP_PROJECT_ID"
            )
            _platform_init_errors.append("vertex-llm-config")

    elif provider == ProviderName.OPENAI:
        if api_key:
            try:
                from common.llm.providers.openai import OpenAILLMProvider

                resolved_model = model_override or LLM_FALLBACK_MODEL_OPENAI
                _platform_llm = OpenAILLMProvider(
                    api_key=api_key,
                    model=resolved_model,
                    base_url=OPENAI_CHAT_BASE_URL,
                    provider_name=ProviderName.OPENAI,
                )
                logger.info("Platform LLM: openai/%s", resolved_model)
            except Exception:
                logger.exception("Failed to initialize platform OpenAI LLM provider")
                _platform_init_errors.append("openai-llm")
        else:
            logger.warning("PLATFORM_LLM_PROVIDER=openai but no PLATFORM_LLM_API_KEY")
            _platform_init_errors.append("openai-llm-config")

    elif provider:
        logger.warning(
            "Unknown PLATFORM_LLM_PROVIDER=%r — no platform LLM will be configured",
            provider,
        )
        _platform_init_errors.append("unknown-llm-provider")

    # ── Embedding ────────────────────────────────────────────────────
    # Delegated to common.embedding so core-worker shares the same
    # singleton implementation. Errors are aggregated below.
    _init_platform_embedding()


def get_platform_llm() -> LLMProvider | None:
    """Return the platform LLM singleton, or None if not configured."""
    return _platform_llm


def get_platform_init_errors() -> list[str]:
    """Aggregate init errors from both LLM and embedding initialisation."""
    return [*_platform_init_errors, *_get_embedding_init_errors()]
