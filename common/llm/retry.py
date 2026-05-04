"""Retry and fallback utilities for LLM provider calls — moved from
``core_api.providers._retry`` (CAURA-595).

Provides:

* ``call_with_retry``: async retry with linear backoff. Same shape as the
  pre-extraction ``_call_with_retry`` from ``memory_enrichment.py``.
* ``call_with_fallback``: 3-tier fallback chain (primary → tenant-resolved
  fallback → fake function).

The core_api re-export shim keeps existing call sites working without
edit. New callers (``common.enrichment.service``, the worker handler in
PR-B) should import from here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from common.llm.constants import LLM_RETRY_ATTEMPTS, LLM_RETRY_DELAY_S
from common.provider_names import ProviderName

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def call_with_retry(
    coro_fn: Callable[[], Coroutine[Any, Any, T]],
    label: str,
    max_attempts: int = LLM_RETRY_ATTEMPTS,
    base_delay: float = LLM_RETRY_DELAY_S,
    timeout: float | None = None,
) -> T:
    """Call *coro_fn* with retry and linear backoff.

    Parameters
    ----------
    coro_fn:
        Zero-argument async callable that produces the coroutine to await.
    label:
        Human-readable label for log messages (e.g. ``"openai-enrich"``).
    max_attempts:
        Total number of attempts before giving up.
    base_delay:
        Base delay in seconds; actual delay = ``base_delay * attempt_number``.
    timeout:
        Optional per-attempt timeout in seconds.

    Raises the last exception if all attempts are exhausted.
    """
    if max_attempts <= 0:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            coro = coro_fn()
            if timeout is not None:
                return await asyncio.wait_for(coro, timeout=timeout)
            return await coro
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                delay = base_delay * (attempt + 1)
                logger.warning(
                    "%s attempt %d/%d failed (%s: %s), retrying in %.1fs",
                    label,
                    attempt + 1,
                    max_attempts,
                    type(exc).__name__,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]


async def call_with_fallback(
    primary_provider_name: str,
    call_fn: Callable[..., Coroutine[Any, Any, T]],
    fake_fn: Callable[[], T],
    tenant_config: object | None = None,
    *,
    service_label: str = "",
    model_override: str | None = None,
    model_attr: str = "enrichment_model",
    timeout: float | None = None,
    provider_factory: Callable[..., Any] | None = None,
) -> T:
    """3-tier fallback chain for LLM calls.

    1. Try *primary_provider_name* via ``call_fn(provider)`` with retry.
    2. If that fails and ``tenant_config.resolve_fallback()`` provides an
       alternative, try the fallback provider.
    3. If everything fails, call *fake_fn()* as the last resort.

    Parameters
    ----------
    primary_provider_name:
        Name of the primary provider (e.g. ``"openai"``, ``"gemini"``).
    call_fn:
        Async callable that takes an ``LLMProvider`` and returns the result.
    fake_fn:
        Synchronous callable returning a safe default (no LLM needed).
    tenant_config:
        Optional tenant configuration with ``resolve_fallback()`` method.
    service_label:
        Human-readable label for log messages.
    model_override:
        If provided, forwarded to the provider factory to override the
        default model. Use this to pass per-service model preferences
        (e.g., ``tenant_config.enrichment_model``).
    model_attr:
        Attribute name forwarded to the provider factory for resolving the
        default Vertex model. Defaults to ``"enrichment_model"``; entity
        extraction should pass ``"entity_extraction_model"``.
    provider_factory:
        Callable ``(name, tenant_config) -> LLMProvider``. Defaults to
        ``common.llm.registry.get_llm_provider`` (imported lazily to avoid
        circular imports).
    """
    if provider_factory is None:
        from common.llm.registry import get_llm_provider

        provider_factory = get_llm_provider

    label = service_label or primary_provider_name

    # Intentional fake/none config — skip straight to heuristic, don't try fallback
    if primary_provider_name in (ProviderName.FAKE, ProviderName.NONE):
        logger.debug(
            "%s: primary provider is '%s', using fake fallback directly",
            label,
            primary_provider_name,
        )
        return fake_fn()

    # --- Step 1: Try primary provider with retry ---
    try:
        provider = provider_factory(
            primary_provider_name,
            tenant_config,
            model_override=model_override,
            model_attr=model_attr,
        )
        if not getattr(provider, "is_fake", False):
            return await call_with_retry(
                lambda: call_fn(provider),
                label=f"{label}-primary",
                timeout=timeout,
            )
        logger.warning(
            "%s: provider '%s' resolved to FakeLLMProvider (no API key). Trying fallback.",
            label,
            primary_provider_name,
        )
    except Exception:
        logger.warning(
            "%s primary provider '%s' failed after retries",
            label,
            primary_provider_name,
            exc_info=True,
        )

    # --- Step 2: Try fallback provider ---
    try:
        if tenant_config is not None and hasattr(tenant_config, "resolve_fallback"):
            fb_provider_name, fb_model = tenant_config.resolve_fallback()
            if fb_provider_name and fb_provider_name != primary_provider_name:
                fb_provider = provider_factory(
                    fb_provider_name,
                    tenant_config,
                    model_override=fb_model,
                    model_attr=model_attr,
                )
                if getattr(fb_provider, "is_fake", False):
                    logger.warning(
                        "%s: fallback provider '%s' also resolved to FakeLLMProvider (no API key).",
                        label,
                        fb_provider_name,
                    )
                else:
                    logger.info(
                        "%s falling back from %s to %s",
                        label,
                        primary_provider_name,
                        fb_provider_name,
                    )
                    return await call_with_retry(
                        lambda: call_fn(fb_provider),
                        label=f"{label}-fallback-{fb_provider_name}",
                        timeout=timeout,
                    )
    except Exception:
        logger.warning(
            "%s fallback resolution/provider also failed",
            label,
            exc_info=True,
        )

    # --- Step 3: Fake function as last resort ---
    logger.warning("All LLM providers failed for %s, using fake fallback", label)
    return fake_fn()
