"""LLM provider registry — moved from ``core_api.providers._registry``
(CAURA-595).

Constructs LLM providers by name with three-tier credential resolution:

    Tenant key  →  Platform singleton  →  FakeLLMProvider

Infrastructure backend factories (storage, job queue, identity, conflict,
STM) stay in ``core_api.providers._registry`` — those are core-api–only
concerns. core-worker only needs LLM construction, so this module is the
narrow public surface both processes share.
"""

from __future__ import annotations

import logging
import os

from common.llm._credentials import (
    resolve_gemini_config,
    resolve_openai_compatible,
)
from common.llm._platform import get_platform_llm
from common.llm.constants import (
    OPENAI_REQUEST_TIMEOUT_SECONDS as _DEFAULT_OPENAI_TIMEOUT,
)
from common.llm.protocols import LLMProvider
from common.llm.providers.fake import FakeLLMProvider
from common.llm.providers.gemini import GeminiLLMProvider
from common.llm.providers.openai import OpenAILLMProvider
from common.provider_names import ProviderName

logger = logging.getLogger(__name__)

_LLM_FAKE_SENTINELS = frozenset({ProviderName.FAKE, ProviderName.NONE})
_OPENAI_COMPATIBLE = frozenset(
    {ProviderName.OPENAI, ProviderName.ANTHROPIC, ProviderName.OPENROUTER}
)


def get_llm_provider(
    name: str | None,
    tenant_config: object | None = None,
    *,
    model_override: str | None = None,
    model_attr: str = "enrichment_model",
) -> LLMProvider:
    """Construct an LLM provider by name.

    Parameters
    ----------
    name:
        Provider identifier: ``"openai"``, ``"anthropic"``, ``"openrouter"``,
        ``"gemini"``, ``"fake"``, or ``"none"``. Pass ``None`` or ``""`` to
        use the platform LLM (or ``FakeLLMProvider`` if no platform LLM is
        configured) — this is the path callers like contradiction detection
        take when neither tenant override nor env default is set.
    tenant_config:
        Optional ``ResolvedConfig`` (or compatible object) for per-tenant
        credential overrides.
    model_override:
        If provided, use this model instead of the default resolved from
        tenant config or global settings. Used by recall service to pass
        ``tenant_config.recall_model``.
    model_attr:
        Attribute name to read from tenant config / global settings when
        resolving the default model. Defaults to ``"enrichment_model"``;
        entity extraction should pass ``"entity_extraction_model"``.

    Raises
    ------
    ValueError
        If ``name`` is a non-empty string that doesn't match any known
        provider. ``None`` / ``""`` are accepted (see ``name``).
    """
    if name in _LLM_FAKE_SENTINELS:
        return FakeLLMProvider()

    # Empty / None provider name → no specific provider requested. This
    # happens when a tenant has no override AND the global env default
    # is unset (e.g. ``ENTITY_EXTRACTION_PROVIDER`` not set on
    # core-api). Without this branch the fall-through would hit the
    # final ``ValueError("Unknown LLM provider: None")`` raise, which
    # ``call_with_fallback`` would then catch into the
    # "primary provider failed after retries" warning path — misleading
    # (no retries actually ran) and skips the platform fallback entirely.
    # Use the platform LLM if configured, fake otherwise. Surfaced live
    # on staging 2026-04-26 when CAURA-595 contradiction-detection
    # fired with ``provider_name=None`` and never reached the
    # configured ``PLATFORM_LLM_*``.
    if not name:
        platform = get_platform_llm()
        if platform is not None:
            logger.info(
                "No primary provider name supplied, using platform LLM (%s)",
                platform.model,
            )
            return platform
        # System-level misconfiguration: neither a primary provider name
        # nor ``PLATFORM_LLM_*`` env vars are set. Fall back to fake so
        # callers don't crash, but log at ERROR so monitoring picks it
        # up — silently producing empty LLM responses in production
        # would mask correctness regressions across every consumer
        # (enrichment, recall, contradiction detection, …).
        logger.error(
            "No primary provider name and no platform LLM configured; "
            "returning FakeLLMProvider"
        )
        return FakeLLMProvider()

    if name in _OPENAI_COMPATIBLE:
        api_key, base_url, model = resolve_openai_compatible(name, tenant_config)
        if not api_key:
            platform = get_platform_llm()
            if platform is not None:
                if model_override:
                    logger.warning(
                        "model_override=%r ignored for provider '%s': falling back to platform LLM singleton (%s)",
                        model_override,
                        name,
                        platform.model,
                    )
                logger.info(
                    "No tenant key for '%s', using platform LLM (%s)",
                    name,
                    platform.model,
                )
                return platform
            logger.warning(
                "No API key for LLM provider '%s', returning FakeLLMProvider",
                name,
            )
            return FakeLLMProvider()
        # Read the timeout from ``os.environ`` at construction time
        # (not via the import-time constant) — ``OPENAI_REQUEST_TIMEOUT_SECONDS``
        # in the env may have been populated by core-api's
        # ``bridge_credentials_to_environ()`` AFTER the constants
        # module was imported, so the import-time default would
        # silently shadow a ``.env``-configured value. The fallback
        # constant matches what the constants module would have
        # produced for the all-defaults case.
        try:
            request_timeout = float(
                os.environ.get(
                    "OPENAI_REQUEST_TIMEOUT_SECONDS",
                    _DEFAULT_OPENAI_TIMEOUT,
                )
            )
        except (TypeError, ValueError):
            request_timeout = _DEFAULT_OPENAI_TIMEOUT
        return OpenAILLMProvider(
            api_key=api_key,
            model=model_override or model,
            base_url=base_url,
            provider_name=name,
            request_timeout_seconds=request_timeout,
        )

    if name == ProviderName.GEMINI:
        api_key, model = resolve_gemini_config(tenant_config, model_attr=model_attr)
        if not api_key:
            platform = get_platform_llm()
            if platform is not None:
                if model_override:
                    logger.warning(
                        "model_override=%r ignored for provider '%s': falling back to platform LLM singleton (%s)",
                        model_override,
                        name,
                        platform.model,
                    )
                logger.info(
                    "No tenant key for 'gemini', using platform LLM (%s)",
                    platform.model,
                )
                return platform
            logger.warning(
                "No API key for Gemini LLM provider, returning FakeLLMProvider",
            )
            return FakeLLMProvider()
        return GeminiLLMProvider(api_key=api_key, model=model_override or model)

    raise ValueError(f"Unknown LLM provider: {name}")
