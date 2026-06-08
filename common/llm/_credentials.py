"""Credential resolution for LLM providers — moved from
``core_api.providers._credentials`` (CAURA-595).

Centralises the (api_key, base_url, model) tuple lookup for OpenAI-
compatible providers and the (api_key, model) tuple for Gemini.
Tenant config is checked first; ``os.environ`` is the fallback (this
replaces the previous ``settings.X`` reads — same env-driven shape
as ``common.embedding._registry``).

Vertex AI credential resolution lives in ``_platform.py`` — Vertex is
only available as a platform-tier singleton configured by enterprise
operators, never as a tenant-selectable provider.
"""

from __future__ import annotations

import logging
import os

from common.llm.constants import (
    ANTHROPIC_CHAT_BASE_URL,
    ANTHROPIC_DEFAULT_MODEL,
    GEMINI_DEFAULT_MODEL,
    LLM_FALLBACK_MODEL_OPENAI,
    OPENAI_CHAT_BASE_URL,
    OPENROUTER_CHAT_BASE_URL,
    OPENROUTER_DEFAULT_MODEL,
)
from common.provider_names import ProviderName

logger = logging.getLogger(__name__)


# Per-provider tenant-config attribute holding the API key. Hoisted to
# module level so ``has_credentials`` doesn't allocate a fresh dict on
# every call (tier-1 health checks hit this on the warm path).
_TENANT_KEY_ATTR: dict[str, str] = {
    ProviderName.OPENAI: "openai_api_key",
    ProviderName.ANTHROPIC: "anthropic_api_key",
    ProviderName.OPENROUTER: "openrouter_api_key",
    ProviderName.GEMINI: "gemini_api_key",
}


def _env_key(provider: str) -> str:
    """Map a ProviderName to the env var that holds its API key."""
    if provider == ProviderName.OPENAI:
        return os.environ.get("OPENAI_API_KEY", "")
    if provider == ProviderName.ANTHROPIC:
        return os.environ.get("ANTHROPIC_API_KEY", "")
    if provider == ProviderName.OPENROUTER:
        return os.environ.get("OPENROUTER_API_KEY", "")
    if provider == ProviderName.GEMINI:
        return os.environ.get("GEMINI_API_KEY", "")
    return ""


def has_credentials(provider: str, tenant_config: object | None = None) -> bool:
    """Check whether credentials are available for *provider*.

    Tenant config first (if provided), then env vars.
    """
    attr_name = _TENANT_KEY_ATTR.get(provider)
    if attr_name is None:
        return False
    if tenant_config is not None:
        return bool(getattr(tenant_config, attr_name, None))
    return bool(_env_key(provider))


def resolve_openai_compatible(
    provider: str,
    tenant_config: object | None = None,
) -> tuple[str, str, str]:
    """Resolve (api_key, base_url, model) for an OpenAI-compatible provider.

    Tenant config first, env-var fallback. Returns empty strings when
    credentials are missing.
    """
    if provider == ProviderName.OPENAI:
        key = (
            (
                getattr(tenant_config, "openai_api_key", None)
                if tenant_config is not None
                else None
            )
            or _env_key(ProviderName.OPENAI)
            or ""
        )
        return key, OPENAI_CHAT_BASE_URL, LLM_FALLBACK_MODEL_OPENAI

    if provider == ProviderName.ANTHROPIC:
        key = (
            (
                getattr(tenant_config, "anthropic_api_key", None)
                if tenant_config is not None
                else None
            )
            or _env_key(ProviderName.ANTHROPIC)
            or ""
        )
        return key, ANTHROPIC_CHAT_BASE_URL, ANTHROPIC_DEFAULT_MODEL

    if provider == ProviderName.OPENROUTER:
        key = (
            (
                getattr(tenant_config, "openrouter_api_key", None)
                if tenant_config is not None
                else None
            )
            or _env_key(ProviderName.OPENROUTER)
            or ""
        )
        return key, OPENROUTER_CHAT_BASE_URL, OPENROUTER_DEFAULT_MODEL

    return "", "", ""


def resolve_gemini_config(
    tenant_config: object | None = None,
    *,
    model_attr: str = "enrichment_model",
) -> tuple[str, str]:
    """Resolve (api_key, model) for the Gemini Developer API.

    Key-auth only — no GCP project/ADC. Tenant config first, env-var
    fallback. Returns empty ``api_key`` when credentials are missing.

    Parameters
    ----------
    model_attr:
        Name of the attribute to read from *tenant_config* for the model
        (e.g. ``"enrichment_model"``, ``"recall_model"``,
        ``"entity_extraction_model"``). Falls back to
        ``GEMINI_DEFAULT_MODEL``.
    """
    key = (
        (
            getattr(tenant_config, "gemini_api_key", None)
            if tenant_config is not None
            else None
        )
        or _env_key(ProviderName.GEMINI)
        or ""
    )
    # Tenant config takes precedence; env var fallback uses the same
    # ``ENTITY_EXTRACTION_MODEL`` shape core-api's ``settings`` exposed.
    model = (
        (
            getattr(tenant_config, model_attr, None)
            if tenant_config is not None
            else None
        )
        or os.environ.get(model_attr.upper())
        or os.environ.get("ENTITY_EXTRACTION_MODEL")
        or GEMINI_DEFAULT_MODEL
    )
    return key, model
