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


# 09/02 M-10 / M-11 — the model chain did not know which provider it was
# resolving for.
#
# ``enrichment_model`` / ``contradiction_model`` / ``recall_model`` are SHARED
# tenant settings: one value, read by whichever provider happens to be active.
# Two separate defects fell out of that.
#
# M-11: ``resolve_openai_compatible`` never accepted ``model_attr`` at all, so
# every per-service model knob was inert on the default provider — a tenant
# could set ``contradiction_model`` and nothing would read it. Only the Gemini
# branch honoured it.
#
# M-10: the reverse. Gemini DOES read the shared attribute, so a tenant who had
# configured ``enrichment_model = "gpt-5.4-nano"`` for OpenAI and then switched
# provider handed Gemini an OpenAI model id — a 404 on every call, which is why
# the documented Gemini setup never worked.
#
# The principled fix is provider-scoped model settings; that is a config
# migration. This is the contained one: recognise when a configured model
# CONFIDENTLY belongs to a different provider family and fall back to the
# target provider's default instead of sending a request that cannot succeed.
#
# Deliberately conservative — it only overrides on a positive match against
# another family's prefixes, never on an unrecognised name. Custom deployments,
# fine-tunes and OpenRouter's ``vendor/model`` ids must keep passing through
# untouched, so "not recognised" means "leave it alone".
_MODEL_FAMILY_PREFIXES: dict[str, tuple[str, ...]] = {
    ProviderName.OPENAI: ("gpt-", "o1", "o3", "o4", "chatgpt", "text-", "davinci"),
    ProviderName.ANTHROPIC: ("claude-",),
    ProviderName.GEMINI: ("gemini-", "models/gemini"),
}


def _model_family(model: str) -> str | None:
    """The provider family a model id confidently belongs to, or None."""
    low = (model or "").strip().lower()
    for family, prefixes in _MODEL_FAMILY_PREFIXES.items():
        if low.startswith(prefixes):
            return family
    return None


def _model_for_provider(
    provider: str,
    tenant_config: object | None,
    model_attr: str,
    default_model: str,
) -> str:
    """Resolve the model for ``provider``, ignoring another family's id.

    Same precedence the Gemini branch already used — tenant attribute, then the
    matching env var, then the provider default — with one addition: a value
    that belongs to a different provider family is discarded with a WARNING
    rather than sent. Silently overriding an operator's explicit setting would
    trade a 404 for a mystery, so the log names both the rejected model and the
    substitute.
    """
    configured = (
        getattr(tenant_config, model_attr, None) if tenant_config is not None else None
    ) or os.environ.get(model_attr.upper())
    # Must be a non-empty STRING. ``getattr`` on a loosely-typed config object
    # can yield anything — a Mock in tests, a sentinel, an int from a
    # mis-parsed env — and a non-string model id is meaningless here and would
    # only fail further down, inside the provider SDK, where the cause is much
    # harder to see.
    if not isinstance(configured, str) or not configured.strip():
        return default_model
    family = _model_family(configured)
    if family is not None and family != provider:
        logger.warning(
            "LLM model %r is a %s model but the active provider is %s; "
            "falling back to %r. Set a %s model for this tenant, or scope the "
            "%s setting per provider.",
            configured,
            family,
            provider,
            default_model,
            provider,
            model_attr,
        )
        return default_model
    return configured


def resolve_openai_compatible(
    provider: str,
    tenant_config: object | None = None,
    *,
    model_attr: str = "enrichment_model",
) -> tuple[str, str, str]:
    """Resolve (api_key, base_url, model) for an OpenAI-compatible provider.

    Tenant config first, env-var fallback. Returns empty strings when
    credentials are missing.

    ``model_attr`` names the tenant setting to read the model from
    (``"enrichment_model"``, ``"contradiction_model"``, ``"recall_model"``, …),
    matching ``resolve_gemini_config``. 09/02 M-11: this parameter did not
    exist, so the per-service model knobs were inert on every OpenAI-compatible
    provider — a tenant could set ``contradiction_model`` and nothing read it.
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
        model = _model_for_provider(
            provider, tenant_config, model_attr, LLM_FALLBACK_MODEL_OPENAI
        )
        return key, OPENAI_CHAT_BASE_URL, model

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
        model = _model_for_provider(
            provider, tenant_config, model_attr, ANTHROPIC_DEFAULT_MODEL
        )
        return key, ANTHROPIC_CHAT_BASE_URL, model

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
        # OpenRouter ids are ``vendor/model`` and deliberately do not match any
        # family prefix, so a configured value passes through untouched.
        model = _model_for_provider(
            provider, tenant_config, model_attr, OPENROUTER_DEFAULT_MODEL
        )
        return key, OPENROUTER_CHAT_BASE_URL, model

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
    #
    # 09/02 M-10: this read the SHARED model attribute unguarded, so a tenant
    # who had configured ``enrichment_model`` for OpenAI and then switched to
    # Gemini handed Gemini an OpenAI model id — a 404 on every call, which is
    # why the documented Gemini setup never worked. ``_model_for_provider``
    # discards a confidently-foreign id and logs what it substituted.
    model = _model_for_provider(
        ProviderName.GEMINI,
        tenant_config,
        model_attr,
        os.environ.get("ENTITY_EXTRACTION_MODEL") or GEMINI_DEFAULT_MODEL,
    )
    return key, model
