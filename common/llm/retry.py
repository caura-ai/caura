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
    non_retryable: tuple[type[BaseException], ...] = (),
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
    non_retryable:
        Exception types that must NOT be retried within this provider — the
        first occurrence raises immediately. Empty by default, so every
        existing caller keeps its current behaviour unchanged.

        OPT-IN, and deliberately not a global classification. The obvious
        version of this — "a ValidationError can't succeed on retry, so never
        retry one" — is WRONG for most callers here. It is only true when the
        call is deterministic, and determinism comes from the CALLER: only
        entity extraction pins a ``seed`` (a CRC32 of its prompt, precisely so
        retries reproduce byte-identical output). Every other caller,
        enrichment included, is unseeded, so a re-ask genuinely may return
        parseable output and its retry is worth having. Classifying globally
        would strip a useful retry from ~10 services to save one wasted call
        on the single deterministic one.

        Keyed on exception TYPE rather than on seeded-ness because a seeded
        call still has non-deterministic failure modes — a network timeout
        must be retried no matter how fixed the seed is. Seeding is what makes
        a SHAPE failure deterministic; the type is what separates shape from
        transport.

        Note this bounds retries WITHIN a provider only. ``call_with_fallback``
        still advances to the fallback provider afterwards, which is correct:
        that is a different model and may well parse what this one mangled.

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
            # ``isinstance(exc, ())`` is False, so an empty tuple makes this a
            # no-op for every caller that has not opted in.
            if isinstance(exc, non_retryable):
                logger.warning(
                    "%s attempt %d/%d failed (%s: %s); NOT retrying — "
                    "caller declared this type deterministic",
                    label,
                    attempt + 1,
                    max_attempts,
                    type(exc).__name__,
                    exc,
                )
                break
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
    max_attempts: int = LLM_RETRY_ATTEMPTS,
    provider_factory: Callable[..., Any] | None = None,
    non_retryable: tuple[type[BaseException], ...] = (),
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
    max_attempts:
        Per-provider attempt count, forwarded to ``call_with_retry`` for both the
        primary and fallback provider. Defaults to ``LLM_RETRY_ATTEMPTS``. Pass
        ``1`` on latency-sensitive read paths (e.g. recall) to fail fast to the
        fallback provider instead of retrying a slow/hung primary.
    provider_factory:
        Callable ``(name, tenant_config) -> LLMProvider``. Defaults to
        ``common.llm.registry.get_llm_provider`` (imported lazily to avoid
        circular imports).
    non_retryable:
        Forwarded to ``call_with_retry`` for BOTH providers — see its docstring
        for why this is opt-in. Applied to the fallback provider too because
        the seed travels with the prompt, so a deterministic failure is
        deterministic there as well; what still runs is the PROVIDER hop, not
        the retry.
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
                max_attempts=max_attempts,
                timeout=timeout,
                non_retryable=non_retryable,
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
    # Record WHY we never reached the fallback, because otherwise this tier can be
    # dead in a deployment and nothing says so: every skip path below is silent,
    # and the only symptom is the step-3 warning, which reads as though every
    # provider had been tried. Measured on prod core-api over 3 days to
    # 2026-08-17: 82 "All LLM providers failed" and ZERO "falling back from" —
    # the tier had never once executed, because only one provider key is set.
    # Names only, never payloads.
    # ``skip_code`` is the queryable one — ``_add_logrecord_extras`` promotes stdlib
    # ``extra`` keys to JSON fields, so an operator can group by it instead of
    # substring-searching prose, which is what answering the question above cost.
    fallback_skipped: tuple[str, str] | None = None
    try:
        if tenant_config is None or not hasattr(tenant_config, "resolve_fallback"):
            fallback_skipped = ("no_tenant_config", "no tenant config exposing resolve_fallback()")
        else:
            fb_provider_name, fb_model = tenant_config.resolve_fallback()
            if not fb_provider_name:
                fallback_skipped = (
                    "no_fallback_configured",
                    # Both levers, because ``resolve_fallback`` tries them in this
                    # order: an explicit ``fallback_llm.provider`` is returned with NO
                    # key check at all, and only if it is unset does it scan for a
                    # keyed provider differing from the primary.
                    "no fallback provider configured — set fallback_llm.provider, or "
                    "supply an API key for a provider other than the primary",
                )
            elif fb_provider_name == primary_provider_name:
                fallback_skipped = (
                    "fallback_is_primary",
                    f"resolved fallback is the primary provider '{primary_provider_name}'",
                )
            else:
                fb_provider = provider_factory(
                    fb_provider_name,
                    tenant_config,
                    model_override=fb_model,
                    model_attr=model_attr,
                )
                if getattr(fb_provider, "is_fake", False):
                    # Already its own warning — not folded into ``fallback_skipped``,
                    # which exists for the paths that had none.
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
                        max_attempts=max_attempts,
                        timeout=timeout,
                        non_retryable=non_retryable,
                    )
    except Exception:
        # Left as-is: this path is already loud, so it needs no skip reason.
        logger.warning(
            "%s fallback resolution/provider also failed",
            label,
            exc_info=True,
        )

    if fallback_skipped is not None:
        skip_code, skip_detail = fallback_skipped
        # Not "the next line is not evidence…": these two are adjacent in this
        # coroutine but interleave with other instances in the aggregated log view,
        # which is exactly where anyone reads them. State it self-containedly.
        logger.warning(
            "%s: fallback provider tier SKIPPED (%s); no second provider was tried",
            label,
            skip_detail,
            extra={"fallback_skip_reason": skip_code},
        )

    # --- Step 3: Fake function as last resort ---
    logger.warning("All LLM providers failed for %s, using fake fallback", label)
    return fake_fn()
