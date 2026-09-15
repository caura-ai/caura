"""LLM provider registry — moved from ``core_api.providers._registry``
(CAURA-595).

Constructs LLM providers by name with three-tier credential resolution:

    Tenant key  →  Platform singleton  →  FakeLLMProvider

Infrastructure backend factories (storage, job queue, identity, conflict,
STM) stay in ``core_api.providers._registry`` — those are core-api-only
concerns. core-worker only needs LLM construction, so this module is the
narrow public surface both processes share.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict

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

# 09/02 M-36 — one pooled client per CONFIGURATION, not per call.
#
# ``OpenAILLMProvider.__init__`` builds an ``httpx.AsyncClient`` with its own
# connection pool (CAURA-627 sized it for bulk-write fan-out). This factory is
# called on EVERY LLM call — ``common/llm/retry.py`` invokes ``provider_factory``
# for the primary provider and again for the fallback — so each call minted a
# fresh pool, and nothing ever closed it. ``OpenAILLMProvider.aclose`` exists and
# its own docstring names this exact failure ("a leak in long-lived processes
# that rotate client instances"); it simply had no caller.
#
# Only the OpenAI-compatible branch is cached, because it is the only provider
# that owns a client — Gemini and Fake hold none, so caching them would buy
# nothing and add a lifetime to reason about.
#
# THE KEY IS THE WHOLE CONFIGURATION, deliberately. ``request_timeout`` is read
# from ``os.environ`` at construction time (see the comment at that call site,
# which explains why it must not use the import-time constant), and the api key
# and model come from tenant config. Putting all of them in the key preserves
# today's behaviour exactly: change any of them and you get a NEW provider, as
# you would have before. A key of just ``name`` would silently pin the first
# tenant's credentials for the life of the process.
#
# The api key goes in the key VERBATIM, and that is deliberate. An earlier
# revision hashed it, on the reasoning that a cache outliving a request should
# not retain a secret. That reasoning was wrong: the value being cached is the
# PROVIDER, and ``OpenAILLMProvider`` holds the same api key in memory for
# exactly as long as the cache holds the tuple. Hashing bought no reduction in
# exposure — only the appearance of one — while adding a collision whose
# consequence is handing one tenant's provider to another. CodeQL flagged the
# hash as ``py/weak-sensitive-data-hashing``, and the right response was to
# delete the hashing rather than suppress the alert or reach for bcrypt: this
# is an equality comparison inside one process, not password verification, and
# a salted slow hash would be both non-deterministic and useless here.
_PROVIDER_CACHE: OrderedDict[tuple, LLMProvider] = OrderedDict()

# Bounded so a many-tenant process cannot grow pools without limit. Eviction is
# LRU, so the hot tenants keep their pools and a rare one pays a reconnect.
_PROVIDER_CACHE_MAX = 32


def _close_evicted(provider: LLMProvider) -> None:
    """Best-effort close of an evicted provider's pool.

    ``get_llm_provider`` is synchronous, so the ``await`` that ``aclose``
    requires cannot happen inline. When a loop is running (every production
    caller is async) the close is scheduled on it; otherwise the provider is
    simply dropped, which is exactly today's behaviour and no worse.

    A failure here must never propagate: this runs on the path that is trying
    to hand a caller a working provider.
    """
    aclose = getattr(provider, "aclose", None)
    if aclose is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(aclose())
    # Keep a strong reference until it finishes — a bare ``create_task`` result
    # can be garbage-collected mid-flight, which would cancel the close.
    _PENDING_CLOSES.add(task)
    task.add_done_callback(_PENDING_CLOSES.discard)


_PENDING_CLOSES: set = set()


def reset_provider_cache() -> None:
    """Drop every cached provider. For tests that swap credentials or env."""
    _PROVIDER_CACHE.clear()


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
        # 09/02 M-11: ``model_attr`` is forwarded here now. Without it the
        # per-service knobs (contradiction_model, dedup_model, recall_model …)
        # resolved to the provider default on every OpenAI-compatible provider,
        # i.e. the settings existed and did nothing.
        api_key, base_url, model = resolve_openai_compatible(
            name, tenant_config, model_attr=model_attr
        )
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
        cache_key = (
            name,
            base_url,
            model_override or model,
            api_key,
            request_timeout,
        )
        cached = _PROVIDER_CACHE.get(cache_key)
        if cached is not None:
            _PROVIDER_CACHE.move_to_end(cache_key)
            return cached

        provider = OpenAILLMProvider(
            api_key=api_key,
            model=model_override or model,
            base_url=base_url,
            provider_name=name,
            request_timeout_seconds=request_timeout,
        )
        _PROVIDER_CACHE[cache_key] = provider
        while len(_PROVIDER_CACHE) > _PROVIDER_CACHE_MAX:
            _, evicted = _PROVIDER_CACHE.popitem(last=False)
            _close_evicted(evicted)
        return provider

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
