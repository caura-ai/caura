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
import random
import time
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar

from common.llm.call_context import llm_call_label
from common.llm.constants import (
    LLM_MAX_RETRY_AFTER_S,
    LLM_RETRY_ATTEMPTS,
    LLM_RETRY_DELAY_S,
    LLM_RETRY_JITTER_FRACTION,
)
from common.provider_names import ProviderName

logger = logging.getLogger(__name__)

T = TypeVar("T")


def retry_after_seconds(exc: BaseException) -> float | None:
    """How long the provider asked us to wait, or ``None`` if it did not.

    Duck-typed on the exception rather than importing a provider SDK: this
    module is shared by the OpenAI-compatible, Gemini and Vertex providers
    and imports none of them. ``openai.APIStatusError`` and
    ``httpx.HTTPStatusError`` both expose ``.response.headers``, which is
    all this needs.

    Reads ``retry-after-ms`` before ``Retry-After``. OpenAI sends both,
    the millisecond form expresses the same intent more precisely, and it
    is the one the SDK's own retry logic preferred — worth keeping now that
    this layer has taken that job over. The two can genuinely disagree at
    the ``LLM_MAX_RETRY_AFTER_S`` boundary, where a hint of 5,200 ms and a
    hint of "5" fall on opposite sides.

    ``Retry-After`` is either delta-seconds or an HTTP-date (RFC 9110
    §10.2.3) and real providers send both, so both are parsed. A date in
    the past clamps to 0 rather than going negative — a clock skew must not
    turn into a negative sleep.

    Returns ``None`` for anything unparseable, which is the same answer as
    "no header": the caller falls back to its own backoff. A provider
    sending a malformed hint should not break the retry it was hinting
    about.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    ms = headers.get("retry-after-ms")
    if ms is not None:
        # Its own guard, not the shared one below: a malformed millisecond
        # header must not discard a well-formed ``Retry-After`` sitting
        # beside it. Preferring the precise header is only an improvement
        # if failing to parse it costs nothing.
        try:
            return max(0.0, float(ms) / 1000.0)
        except (TypeError, ValueError):
            pass
    try:
        raw = headers.get("retry-after")
        if raw is None:
            return None
        raw = raw.strip()
        # Try delta-seconds first: it is what providers actually send, and
        # ``parsedate_to_datetime`` accepts some bare numbers as dates.
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
        when = parsedate_to_datetime(raw)
        # A date without a zone is UTC by RFC 9110; ``parsedate_to_datetime``
        # returns it naive, and subtracting an aware datetime would raise.
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    except (AttributeError, TypeError, ValueError):
        return None


async def call_with_retry(
    coro_fn: Callable[[], Coroutine[Any, Any, T]],
    label: str,
    max_attempts: int = LLM_RETRY_ATTEMPTS,
    base_delay: float = LLM_RETRY_DELAY_S,
    timeout: float | None = None,
    non_retryable: tuple[type[BaseException], ...] = (),
    budget_s: float | None = None,
) -> T:
    """Call *coro_fn* with retry and linear backoff — see
    :func:`_call_with_retry_impl` for the full parameter contract.

    This thin wrapper additionally publishes *label* as the ambient
    :data:`common.llm.call_context.llm_call_label` for the duration of the
    call (E4-prep), so the provider's per-call token log can attribute
    cost to its service without any signature changes. Reset in
    ``finally`` — the label must not leak into a sibling task's log lines.
    """
    _label_token = llm_call_label.set(label)
    try:
        return await _call_with_retry_impl(
            coro_fn,
            label,
            max_attempts=max_attempts,
            base_delay=base_delay,
            timeout=timeout,
            non_retryable=non_retryable,
            budget_s=budget_s,
        )
    finally:
        llm_call_label.reset(_label_token)


async def _call_with_retry_impl(
    coro_fn: Callable[[], Coroutine[Any, Any, T]],
    label: str,
    max_attempts: int = LLM_RETRY_ATTEMPTS,
    base_delay: float = LLM_RETRY_DELAY_S,
    timeout: float | None = None,
    non_retryable: tuple[type[BaseException], ...] = (),
    budget_s: float | None = None,
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
    budget_s:
        Optional wall-clock budget for this whole call — every attempt plus
        every delay between them. PER PROVIDER, exactly like *timeout*:
        ``call_with_fallback`` forwards the same value to the primary and to
        the fallback, so two providers means two budgets.

        Given one, three things stop being guesses. Each attempt's timeout
        becomes ``min(timeout, remaining)`` so an attempt cannot overrun the
        budget; the loop stops once the budget is spent instead of starting
        an attempt with no time to finish; and the cap on a ``Retry-After``
        is DERIVED from what is left rather than read from
        ``LLM_MAX_RETRY_AFTER_S``, which exists precisely because callers
        without a declared budget give this layer nothing to reason from.

        Why it matters more than tidiness: without it the worst case is
        enforced by parameter VALUES rather than by structure. ``recall``
        documents "one primary attempt (15s), then one fallback attempt
        (15s) ... worst case ~30s" — a guarantee that holds only while
        ``max_attempts`` stays 1, and that silently doubles if anyone
        restores the default. A budget makes the same promise hold whatever
        the attempt count is.

        Omitting it keeps the previous behaviour exactly, which is right for
        the callers that have no deadline of their own to declare.
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
    if budget_s is not None and budget_s <= 0:
        # Same reasoning as the guard above: a caller asking for a
        # zero-length budget has misconfigured something, and silently
        # running one unbounded attempt would hide it behind exactly the
        # overrun the budget was meant to prevent.
        raise ValueError(f"budget_s must be > 0, got {budget_s}")
    # ``monotonic``, not ``time()``: a deadline must not move because NTP
    # stepped the wall clock mid-retry.
    deadline = None if budget_s is None else time.monotonic() + budget_s
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            # Checked BEFORE ``coro_fn()`` so no coroutine is created that
            # never gets awaited. Reachable without any sleep: an attempt
            # that runs to its own timeout can exhaust the budget by itself.
            logger.warning(
                "%s giving up before attempt %d/%d — the %.1fs budget is spent",
                label,
                attempt + 1,
                max_attempts,
                budget_s,
            )
            # This is the ONLY break that can be reached with nothing caught
            # yet — the other two are inside the ``except`` below, where
            # ``last_exc`` is always set. On the first iteration it would
            # leave the ``raise`` at the end of this function executing
            # ``raise None``, which Python reports as "exceptions must derive
            # from BaseException": a confusing type error standing in for a
            # perfectly clear condition.
            #
            # Only synthesised when there is nothing to report. If an earlier
            # attempt failed for a real reason, THAT exception is the useful
            # one and running out of time afterwards must not overwrite it —
            # same precedence the retry paths below apply.
            if last_exc is None:
                last_exc = TimeoutError(
                    f"{label}: the {budget_s:.1f}s budget was spent before any "
                    "attempt could start"
                )
            break
        try:
            coro = coro_fn()
            # The budget binds each attempt too, not just the loop. Without
            # this an attempt could start with 2 s left and run for its full
            # ``timeout``, so the budget would be advisory — enforced only by
            # whoever cancels us from outside.
            attempt_timeout = timeout
            if remaining is not None:
                attempt_timeout = (
                    remaining if timeout is None else min(timeout, remaining)
                )
            if attempt_timeout is not None:
                return await asyncio.wait_for(coro, timeout=attempt_timeout)
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
                # What the provider asked for beats what we guessed — but
                # only upward. A hint SHORTER than our own backoff would
                # have us hammer a provider that is already refusing, so
                # ``max`` keeps both floors: never sooner than we planned,
                # never sooner than we were told.
                asked = retry_after_seconds(exc)
                if asked is not None:
                    # Derived when there is a budget to derive from, and only
                    # then falling back to the fixed constant. Half of what
                    # is left, so honouring a wait always leaves at least as
                    # much time again for the attempt it precedes — waiting
                    # out a hint and then having no time to use it is the
                    # same wasted request as not waiting at all.
                    #
                    # This can exceed ``LLM_MAX_RETRY_AFTER_S``, deliberately.
                    # That constant is the answer for callers that declared
                    # nothing; a caller that declares 60 s has said it can
                    # afford to wait, and second-guessing it would make the
                    # budget decorative.
                    now = time.monotonic()
                    cap = (
                        LLM_MAX_RETRY_AFTER_S
                        if deadline is None
                        else max(0.0, (deadline - now) / 2)
                    )
                    if asked > cap:
                        # Sleeping this out is worse than not retrying: the
                        # wait spends the window and the outer timeout fires
                        # with nothing to show. Giving up HERE is not giving
                        # up on the call — ``call_with_fallback`` hops to the
                        # second provider, which is the right answer to "this
                        # one is rate-limited for the next minute".
                        logger.warning(
                            "%s attempt %d/%d failed (%s: %s); provider asked "
                            "for %.1fs, over the %.1fs we can wait (%s) — NOT "
                            "retrying it, so the fallback provider is tried "
                            "instead",
                            label,
                            attempt + 1,
                            max_attempts,
                            type(exc).__name__,
                            exc,
                            asked,
                            cap,
                            "half the remaining budget"
                            if deadline is not None
                            else "no budget declared, so the fixed cap",
                        )
                        break
                    delay = max(delay, asked)
                # Jitter AFTER the max, so it can only ever lengthen the
                # wait — decorrelating must not undercut a Retry-After.
                # ``random`` rather than ``secrets``: this schedules a
                # sleep, it does not protect anything.
                #
                # The fraction is floored again HERE, not only where the
                # env var is read. "Additive only" is the invariant this
                # ordering exists to provide, so it belongs at the point
                # that relies on it — a negative fraction would make
                # ``uniform`` subtract, and the guarantee would be gone
                # however the value arrived.
                jitter_fraction = max(0.0, LLM_RETRY_JITTER_FRACTION)
                jittered = delay + random.uniform(0.0, jitter_fraction * delay)
                # The plain linear delay needs the same check the Retry-After
                # path gets from its cap: a 2 s backoff with 1 s left is a
                # sleep that guarantees the next attempt has nothing. Only
                # the loop-top check would catch that, and only AFTER
                # sleeping the budget away for no reason.
                if deadline is not None and time.monotonic() + jittered >= deadline:
                    logger.warning(
                        "%s attempt %d/%d failed (%s: %s); a %.1fs wait would "
                        "spend the rest of the %.1fs budget — NOT retrying it, "
                        "so the fallback provider is tried instead",
                        label,
                        attempt + 1,
                        max_attempts,
                        type(exc).__name__,
                        exc,
                        jittered,
                        budget_s,
                    )
                    break
                logger.warning(
                    "%s attempt %d/%d failed (%s: %s), retrying in %.1fs%s",
                    label,
                    attempt + 1,
                    max_attempts,
                    type(exc).__name__,
                    exc,
                    jittered,
                    f" (provider asked for {asked:.1f}s)" if asked is not None else "",
                )
                await asyncio.sleep(jittered)
    raise last_exc  # type: ignore[misc]


def deliberate_fake_provider(provider_name: str | None) -> bool:
    """Which of ``call_with_fallback``'s two ``fake_fn`` contexts is this?

    ``fake_fn`` is invoked for two situations that are NOT the same, and callers
    that conflate them ship a dev stub's output to production:

    * The operator explicitly configured ``fake`` — dev or CI. The stub IS the
      intent; it is often the only way a pipeline gets end-to-end coverage without
      an API key.
    * A REAL provider was configured and every attempt failed, or its key is
      missing so it resolved to ``FakeLLMProvider``. That is an outage or a
      misconfigured deployment, not a request for made-up output.

    ``fake`` ONLY, deliberately not ``none``: "none" asks for the feature to be
    off, which is an abstain. ``entity_extraction`` draws the same line — empty
    graph for ``none``, heuristic for ``fake``.

    Callers that persist their result should branch on this and take their own
    "nothing came back" path in the outage case. See ``contradiction_detector``
    (abstains rather than guessing a verdict that marks memories ``conflicted``),
    ``crystallizer_service`` and ``insights_service``.
    """
    return provider_name == ProviderName.FAKE


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
    budget_s: float | None = None,
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
    budget_s:
        Forwarded to ``call_with_retry`` for BOTH providers, so it is a budget
        PER PROVIDER rather than for the chain — the same semantics *timeout*
        already has. Two providers at ``budget_s=15`` means a ~30 s worst case
        before ``fake_fn``, which is exactly the arithmetic ``recall`` spells
        out by hand today.
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
                budget_s=budget_s,
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
                        budget_s=budget_s,
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
