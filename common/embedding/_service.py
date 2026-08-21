"""Service-level embedding entrypoints with retry + degraded-state stats.

Reads provider selection from env (``EMBEDDING_PROVIDER``) when no
tenant override is supplied — both core-api and core-worker drive the
same code path, just with / without ``tenant_config``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack

from common.embedding._registry import get_embedding_provider
from common.embedding.constants import (
    EMBEDDING_BACKGROUND_MAX_CONCURRENCY,
    EMBEDDING_BUDGET_MARGIN_S,
    EMBEDDING_GATE_TIMEOUT_SECONDS,
    EMBEDDING_MAX_CONCURRENCY,
    EMBEDDING_RETRY_ATTEMPTS,
    EMBEDDING_RETRY_DELAY_S,
)
from common.embedding.protocols import InstructionAwareEmbedder

logger = logging.getLogger(__name__)


class _EmbeddingStats:
    """Track failure rate to surface a degraded-provider signal in logs.

    Three consecutive failures fire a single ERROR log (per cycle) so a
    sustained provider outage shows up loudly without spamming once-per-
    request. Reset on the next success.

    Bulk calls additionally keep their OWN streak, which only a bulk
    success clears — see :meth:`record_failure` for the outage the shared
    streak structurally cannot see.
    """

    def __init__(self, label: str = "unknown") -> None:
        # Which backend these numbers belong to. Carried on the instance so
        # every log line this object emits names its own backend — stats can
        # be keyed perfectly and still be unactionable if the alert reads the
        # same for all 256 of them.
        self.label = label
        self.failures = 0
        self.successes = 0
        self.last_failure_time = 0.0
        self.consecutive_failures = 0
        self.consecutive_bulk_failures = 0
        self._lock = asyncio.Lock()

    @staticmethod
    def _is_report_point(streak: int) -> bool:
        """Report on first detection (3) then every 10th (13, 23, 33, …).

        Loud enough to alert on a fresh outage, quiet enough not to spam
        during a sustained one — a 10x reduction, NOT a fixed bound:
        reports still grow linearly with a condition that recurs on every
        request, so this caps the slope, not the total. The next success
        resets the streak, so a later outage re-fires from 3.
        """
        return streak >= 3 and (streak - 3) % 10 == 0

    async def record_success(self, *, bulk: bool = False) -> None:
        """*bulk* additionally clears the bulk-only streak."""
        async with self._lock:
            self.successes += 1
            self.consecutive_failures = 0
            if bulk:
                self.consecutive_bulk_failures = 0

    async def record_failure(self, *, bulk_batch_size: int | None = None) -> None:
        """Count a failure and report at :meth:`_is_report_point`.

        Passing *bulk_batch_size* marks this as a failed BULK call: it
        advances a second streak that only a bulk success clears, and
        reports it separately.

        That second streak exists because the shared one cannot see a
        bulk-only outage. Every path through this module shares one
        ``_EmbeddingStats`` PER BACKEND, so a service that also serves
        search traffic records a success per query embed, which resets
        ``consecutive_failures`` and holds it under the threshold no
        matter how many bulk calls fail in between. Not hypothetical:
        prod TEI rejected 100% of bulk embeds for 30+ days on a
        batch-size cap and "Embedding service degraded" never fired once,
        because query embeds kept clearing the streak.

        What the bulk report adds over the callers' own per-batch ERROR
        (which already carries the provider's traceback) is the CONSECUTIVE
        count: N failures with no bulk success in between distinguishes
        "systematically broken" from "occasionally flaky", which a stream
        of independent tracebacks cannot. It is not the per-occurrence
        detector — the callers' ERROR is — so WARNING, not ERROR: by
        contract this cascades into the per-item fallback, which usually
        still persists correct embeddings. Degradation, not data loss.

        *bulk_batch_size* is the size the CALLER asked for, which is not
        necessarily the size of any single request — the provider splits
        oversized requests to its own backend's cap. The message says so
        rather than naming a number: this class is provider-agnostic (Fake
        and Local never chunk; the OpenAI provider's cap is 32 self-hosted
        but 2048 hosted), so quoting one constant here would be wrong for
        most backends and would send an operator to re-tune a knob their
        deployment does not use — the same misdirection in the opposite
        direction. The size is still worth reporting: it identifies which
        caller is affected (bulk write at 100 vs re-embed at 50) without
        expanding a traceback.

        Both streaks are per BACKEND — see ``_stats_by_scope``. An earlier
        version of this class was process-wide, which meant one tenant's
        healthy bulk success cleared a different tenant's broken backend:
        the same masking this method exists to fix, one level up.
        """
        async with self._lock:
            self.failures += 1
            self.consecutive_failures += 1
            self.last_failure_time = time.monotonic()
            if bulk_batch_size is not None:
                self.consecutive_bulk_failures += 1
                if self._is_report_point(self.consecutive_bulk_failures):
                    logger.warning(
                        "Bulk embedding failing [%s]: %d consecutive bulk "
                        "call(s) "
                        "failed (requested batch=%d), cascading to the "
                        "per-item fallback. Embeddings may still be correct; "
                        "batching is not. The provider already splits a "
                        "request to its own backend's cap, so a batch-size "
                        "rejection is unlikely — look at auth, network, "
                        "quota and the callers' timeouts.",
                        self.label,
                        self.consecutive_bulk_failures,
                        bulk_batch_size,
                    )
            if self._is_report_point(self.consecutive_failures):
                logger.error(
                    "Embedding service degraded [%s]: %d consecutive "
                    "failures (total: %d/%d)",
                    self.label,
                    self.consecutive_failures,
                    self.failures,
                    self.failures + self.successes,
                )


# Stats are keyed PER BACKEND, not one set per process.
#
# A single shared object was the masking bug one level up from the one the
# bulk streak fixes. One process serves many tenants and holds an embedding
# provider per (api_key, model, base_url, …) — ``_registry`` caches up to 32 of
# them precisely because "the same api_key can host multiple simultaneous
# providers, e.g. real OpenAI for one tenant and a local TEI sidecar". Against
# one shared object, a healthy backend's success clears a broken backend's
# streak and the broken one never reports.
#
# ``common/ranking`` reached this first for its log-once dedup — see
# ``_permanent_scope`` and the note there on why clearing must be per backend
# rather than wholesale.
_stats_by_scope: dict[str, _EmbeddingStats] = {}

# Bound it, same reasoning as ranking's ``_PERMANENT_LOGGED_MAX``: the steady
# state is one entry per live backend, but a long-lived process that rotates
# tenant config accumulates a dead entry per retired one. On overflow drop the
# whole map rather than track recency — the only cost is that a still-broken
# backend restarts its streak, which is the safe direction to fail.
#
# DELIBERATELY ABOVE ``_registry._OPENAI_CACHE_MAX`` (256), not equal to it.
# Equal caps would mean a deployment running at the provider cache's own
# ceiling wipes this map routinely — resetting streaks for backends that are
# still live and possibly still broken, which is the one case the wipe should
# not touch. At 2x, every cached backend fits with headroom, so the clear is
# reached only under churn well past what the registry itself retains.
_STATS_SCOPE_MAX = 512


def _stats_scope(provider: object | None, provider_name: str) -> str:
    """Which backend's stats a call belongs to.

    Reads the provider's own ``dedup_scope``, exactly as
    ``common/ranking``'s ``_permanent_scope`` does. That indirection is
    load-bearing rather than stylistic: only the provider can identify its
    own backend. A scope built out here from ``provider_name`` and ``model``
    would name a CONFIG TYPE, not a backend — two tenants on the same model
    behind different keys, or two self-hosted sidecars both serving
    ``bge-m3`` at different URLs, would collide and mask each other, which is
    the whole bug this keying exists to fix.

    Falls back to the provider name for a third-party provider that declares
    no scope: coarse per-name grouping is worse than per-backend but better
    than one global bucket.

    *provider* is ``None`` when construction failed outright (registry
    misconfiguration). The resolved NAME is then the only identity available,
    and every tenant misconfiguring the same provider sharing one streak is
    right — the fault is the config, not a backend.
    """
    if provider is None:
        return provider_name
    scope = getattr(provider, "dedup_scope", None)
    if scope:
        return str(scope)
    return str(getattr(provider, "provider_name", "") or provider_name)


def _stats_label(provider: object | None, provider_name: str) -> str:
    """Human-facing backend identity for the log lines.

    Deliberately separate from :func:`_stats_scope`: the scope must be a
    stable opaque key with no credential in it, which makes it useless to
    read in an alert. The label is the readable half — model and endpoint,
    never the key.
    """
    if provider is None:
        return provider_name
    return str(getattr(provider, "backend_label", "") or provider_name)


def _stats_for(scope: str, label: str) -> _EmbeddingStats:
    """The stats for one backend, created on first use."""
    stats = _stats_by_scope.get(scope)
    if stats is None:
        if len(_stats_by_scope) >= _STATS_SCOPE_MAX:
            _stats_by_scope.clear()
        stats = _EmbeddingStats(label)
        _stats_by_scope[scope] = stats
    return stats

# One-shot misconfiguration log dedup. The registry raises ``ValueError``
# on env-var misconfig; ``_resolve_provider_or_degrade`` catches it,
# records a failure stat, and returns ``None``. That guard runs on
# every embed/query request, so logging the ERROR unconditionally
# would spam the log at request rate. Keyed on the resolved provider
# name (``"openai"``, ``"vertex"``, …) so a multi-tenant deployment
# misconfiguring multiple providers still warns once per provider.
# Module-level (process-scoped); restart resets the set, which is
# the right cadence for "operator forgot a flag" errors.
_misconfiguration_logged: set[str] = set()


# Per-event-loop so a test that spins a fresh loop (or an app that
# restarts one) doesn't reuse a semaphore bound to a dead loop — waiters
# on the stale object would never be woken. Keyed on identity rather than
# reset explicitly because there is no shutdown hook this deep in the
# stack.
_gate: asyncio.Semaphore | None = None
_gate_loop: asyncio.AbstractEventLoop | None = None
_bg_gate: asyncio.Semaphore | None = None
_bg_gate_loop: asyncio.AbstractEventLoop | None = None


class EmbeddingGateTimeout(TimeoutError):
    """No slot freed within ``EMBEDDING_GATE_TIMEOUT_SECONDS``.

    A distinct type because this is a CAPACITY signal, not a provider
    fault — the provider never saw the call. Conflating the two is not
    cosmetic: it makes the retry path amplify the very saturation it is
    reacting to, since every retry queues another waiter for another full
    gate timeout, and it credits the provider's degraded-streak with
    failures it did not cause.

    Subclasses :class:`TimeoutError` so the existing handlers keep working
    unchanged — a gate timeout has always surfaced as one (see
    ``parallel_embed_entity_boost``, ``parallel_embed_enrich``), and callers
    that only want "the embed didn't happen" still need no changes. Only
    code that must tell saturation apart from failure catches this.
    """


class EmbeddingBackendBusy(Exception):
    """The backend refused the call because it is at capacity (HTTP 429).

    The same class of signal as :class:`EmbeddingGateTimeout`, one layer
    out: our gate says "this PROCESS is at its cap", a 429 says "the
    SHARED backend is". Both are capacity, neither is a fault, and
    retrying either deepens it.

    Worth naming separately because a 429 is the only evidence any single
    process can ever have about aggregate demand. The concurrency cap is
    per process, so ``cap x instances`` is what actually arrives at the
    backend and no instance can observe that number — but a 429 can only
    happen once the other instances have taken the capacity, so it
    reports the aggregate directly. It is the signal an aggregate cap
    would have to be built on, which is why it must not be swallowed into
    the generic provider-failure path (where it was: a 429 counted
    against the degraded-provider streak, and was retried).

    Deliberately NOT a ``TimeoutError`` subclass, unlike its sibling. A
    gate timeout is one — we really did wait — and existing handlers were
    already written for that. A refusal is immediate, and callers that
    special-case timeouts should not silently inherit this.
    """


def _is_backend_busy(exc: BaseException) -> bool:
    """Whether *exc* is the backend declining for capacity (HTTP 429).

    Duck-typed on the status code rather than importing the provider SDK:
    this is the service layer, it is shared by every provider, and it has
    no ``openai`` import today. ``openai.APIStatusError`` exposes
    ``status_code`` directly and ``httpx.HTTPStatusError`` exposes it via
    ``.response``, so both shapes are covered without either dependency.

    Only 429 counts, and the narrowness is the point. Cloud Run returns
    429 for "no instance available" and TEI returns it when its queue is
    full, so a 429 unambiguously means "at capacity, try later". A 503 is
    the ambiguous one — it is equally what a genuine outage looks like —
    and classifying it as capacity would suppress the degraded-provider
    ERROR during a real one. A 503 therefore stays a provider failure:
    retried, counted, and loud.
    """
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    return code == 429


def _concurrency_gate() -> asyncio.Semaphore:
    """The process-wide cap on concurrent provider calls.

    See ``EMBEDDING_MAX_CONCURRENCY`` for why the cap exists.
    """
    global _gate, _gate_loop
    loop = asyncio.get_running_loop()
    if _gate is None or _gate_loop is not loop:
        _gate = asyncio.Semaphore(EMBEDDING_MAX_CONCURRENCY)
        _gate_loop = loop
    return _gate


def _background_gate() -> asyncio.Semaphore:
    """The tighter cap that only document/bulk embeds are held to.

    Sized at ``EMBEDDING_BACKGROUND_MAX_CONCURRENCY`` so
    ``EMBEDDING_INTERACTIVE_RESERVED_SLOTS`` of the shared cap stay reachable by
    search-side query embeds during a write flood. Same per-event-loop
    rebinding rationale as :func:`_concurrency_gate`.
    """
    global _bg_gate, _bg_gate_loop
    loop = asyncio.get_running_loop()
    if _bg_gate is None or _bg_gate_loop is not loop:
        _bg_gate = asyncio.Semaphore(EMBEDDING_BACKGROUND_MAX_CONCURRENCY)
        _bg_gate_loop = loop
    return _bg_gate


async def call_embedding_gated[T](
    make_call: Callable[[], Awaitable[T]], *, background: bool
) -> T:
    """Run *make_call* holding a concurrency slot.

    The slot is acquired per ATTEMPT, not per retry sequence, so a
    backing-off retry never squats on a slot another caller could use.

    A slot that doesn't free within ``EMBEDDING_GATE_TIMEOUT_SECONDS``
    raises :class:`EmbeddingGateTimeout` rather than letting waiters
    accumulate unboundedly and stall the write path. It is a
    ``TimeoutError`` subclass, so callers that only care that the embed
    didn't happen need no change; the distinct type exists so the retry and
    stats paths can tell "we queued" apart from "the backend failed",
    which they must, because retrying the former deepens it.

    *background* marks document/bulk work, which must additionally fit
    inside ``EMBEDDING_BACKGROUND_MAX_CONCURRENCY`` so a write burst can
    never consume the slots reserved for search-side query embeds. Query
    embeds pass ``background=False`` and draw on the full cap.

    Deadlock-free by arithmetic, not by luck: background takes the
    background gate BEFORE the shared one, and the reserved slice is the
    exact difference between the two caps. Even with query embeds holding
    every reserved slot, the shared slots still free are
    ``cap - reserved`` — precisely the background gate's size — so every
    background holder can still acquire one. Query embeds only ever take
    the shared gate, so they cannot be blocked behind background work.

    **Never nest this call.** :func:`get_embedding`,
    :func:`get_embeddings_batch` and :func:`get_query_embedding` already
    gate internally, so wrapping one of them deadlocks under saturation:
    the outer hold occupies a shared slot while the inner acquire waits
    for one, and once ``EMBEDDING_MAX_CONCURRENCY`` callers are inside,
    none can proceed and each burns the full timeout. Callers that hold an
    :class:`EmbeddingProvider` directly — core-worker's deferred-embed
    consumer is the only one — call this instead of, never in addition to,
    those three. The hazard is reachable rather than theoretical: the
    registry can hand back the platform singleton itself
    (:func:`common.embedding._registry.get_embedding_provider`), so one
    provider object is reachable by both routes, which is also why the
    gate belongs at the call site and not inside the provider.
    """
    # ``AsyncExitStack`` rather than hand-rolled release bookkeeping: it
    # unwinds on ANY escape — TimeoutError, CancelledError, or a failure
    # inside make_call — so a background caller that took its own slot but
    # not the shared one can never leak it. Leaking one per abandoned wait
    # would erode the background budget to zero and strangle writes, which
    # would look exactly like the saturation this reservation fixes.
    # Unwind is LIFO: shared slot first, then background.
    async with AsyncExitStack() as stack:
        gate = _concurrency_gate()
        # Background work is ALWAYS held to the background gate, even when
        # the reservation is 0 and the two caps are equal — one uncontended
        # acquire (~0.3 us) is not worth an Optional branching through every
        # release path.
        bg = _background_gate() if background else None
        # Log saturation explicitly. During the 2026-07-27 incident the
        # backend reported 3.5 ms inference while callers timed out and
        # nothing said where the time went. ``background`` is a field rather
        # than two distinct message strings so one query aggregates both
        # classes.
        if gate.locked() or (bg is not None and bg.locked()):
            logger.debug(
                "Embedding concurrency gate saturated (background=%s, cap=%d, "
                "background cap=%d); queueing",
                background,
                EMBEDDING_MAX_CONCURRENCY,
                EMBEDDING_BACKGROUND_MAX_CONCURRENCY,
            )
        try:
            async with asyncio.timeout(EMBEDDING_GATE_TIMEOUT_SECONDS):
                # Background takes its own gate BEFORE the shared one. This
                # order is required, not incidental: reversed, background
                # would squat on a shared slot while waiting for its own
                # budget, which is the starvation being prevented.
                if bg is not None:
                    await stack.enter_async_context(bg)
                await stack.enter_async_context(gate)
        except TimeoutError as exc:
            logger.warning(
                "Embedding concurrency gate timeout after %.1fs (background=%s, "
                "cap=%d, background cap=%d) — capacity signal, not a provider "
                "fault; the backend never saw this call",
                EMBEDDING_GATE_TIMEOUT_SECONDS,
                background,
                EMBEDDING_MAX_CONCURRENCY,
                EMBEDDING_BACKGROUND_MAX_CONCURRENCY,
            )
            raise EmbeddingGateTimeout(
                f"embedding gate timeout after {EMBEDDING_GATE_TIMEOUT_SECONDS:.1f}s "
                f"(background={background})"
            ) from exc
        # Classify the backend's answer HERE, at the one choke point every
        # embed passes through, rather than at each call site. Same
        # reasoning as putting the gate here (see the note above on
        # ``call_embedding_gated`` being the single entry): a caller that
        # forgets the classification would silently fall back to treating
        # saturation as a fault, which is the bug being fixed, and there
        # is no way to notice from the call site.
        try:
            return await make_call()
        except Exception as exc:
            if not _is_backend_busy(exc):
                raise
            # WARNING, not debug: unlike our own gate saturating, this
            # says the shared backend ran out, which is the only
            # aggregate-demand signal that exists (see
            # ``EmbeddingBackendBusy``). It has never fired in prod — TEI
            # served 345,802 requests in the 7 days to 2026-08-21 with
            # zero 429s — so if it starts, that is new information and
            # should be visible without turning debug on.
            #
            # The provider's own text is carried rather than left to the
            # exception chain. This has never fired, so the first time it
            # does the operator's question is "was that really a 429, or
            # did the classifier catch something else" — and the answer
            # has to be in the line that alerts, not in a traceback a
            # level up. Not ``exc_info``: a genuine capacity event fires
            # this per refused call, and the gate-timeout precedent is
            # 1,915 in one hour.
            logger.warning(
                "Embedding backend refused at capacity (429, background=%s, "
                "per-process cap=%d) — the shared backend is full, not faulty; "
                "not retrying. Backend said: %s",
                background,
                EMBEDDING_MAX_CONCURRENCY,
                exc,
            )
            raise EmbeddingBackendBusy(
                f"embedding backend at capacity (background={background})"
            ) from exc


def is_blank_text(text: str) -> bool:
    """Whether *text* carries nothing for a model to encode.

    Whitespace counts as blank, not just the empty string: a lone space is
    equally unembeddable and reaches the backend as the same validation
    error.
    """
    return not text or not text.strip()


def _resolve_provider_name(tenant_config: object | None) -> str:
    """Tenant override first, else ``EMBEDDING_PROVIDER`` env, else ``"fake"``."""
    if tenant_config is not None:
        name = getattr(tenant_config, "embedding_provider", None)
        if name:
            return name
    return os.environ.get("EMBEDDING_PROVIDER", "fake")


async def get_embeddings_batch(
    texts: list[str],
    tenant_config: object | None = None,
    *,
    budget_s: float | None = None,
    background: bool,
) -> list[list[float]]:
    """Generate embeddings for multiple texts in a single API call.

    Raises on any provider-side error. Both bulk callers wrap this in
    ``try: ... except Exception:``, so any exception type is acceptable
    here — what matters is that the failure stats counter increments so
    the registry-level degraded-provider trip-wire fires consistently
    with the single-embed paths (``get_embedding`` /
    ``get_query_embedding``).

    What they do with the exception differs, and it is worth being exact
    because an earlier version of this docstring credited both with a
    per-item retry:

    * ``_reembed_batch_via_provider`` DOES fan the batch out per item,
      through the inline/deferred router — so in deferred mode a rejected
      batch becomes N durable EMBED_REQUESTED messages that drain at
      core-worker's sequential rate. That is the demand smoothing, and it
      is why a 429 here needs no special caller handling.
    * ``create_memories_bulk`` does NOT. Inline it re-raises (504 on a
      timeout); otherwise it logs and leaves ``embedding=NULL`` for the
      backfill sweep to collect. Durable either way, but there is no
      second attempt in this request.

    A failed ``embed_batch`` also advances a bulk-only failure streak that
    single-embed successes cannot reset — see ``record_failure`` for why
    that is needed and for the incident behind it.

    Note this is the SERVICE layer: a request larger than the backend's
    accepted batch is split inside the provider, which is the only layer
    that knows its own cap, so *texts* has no length limit imposed here.

    *budget_s* is the caller's OWN deadline, and passing it is what keeps a
    slow backend attributable. Callers enforce their budgets by cancelling
    us, and a cancellation says only "time ran out" — not which layer ate
    it. Given the budget, this bounds the provider call at
    ``budget_s - EMBEDDING_BUDGET_MARGIN_S`` so the inner cap fires first
    and raises ``TimeoutError`` from HERE, naming the embed as the thing
    that overran. Without it the provider is free to sit for
    ``OPENAI_REQUEST_TIMEOUT_SECONDS`` (25 s), which already exceeds the 8 s
    ``BULK_STRONG_EMBED_TIMEOUT_SECONDS`` outright — that budget could never
    be the one to fire. Same principle the concurrency gate applies; see
    ``EMBEDDING_GATE_TIMEOUT_SECONDS``.

    Omitting it keeps the previous behaviour, which is right for callers
    that have no deadline of their own to be under.

    Two error shapes are explicitly accounted for:

    1. ``ValueError`` from ``get_embedding_provider`` — env-var
       misconfiguration (e.g. ``OPENAI_EMBEDDING_BASE_URL`` ⊕
       ``SEND_DIMENSIONS`` mismatch). Used to propagate as an unhandled
       exception that bulk callers caught generically but bypassed
       ``_stats.record_failure``, so the trip-wire never fired under
       sustained misconfig. Now records a failure and re-raises. Counted
       as a plain failure, NOT a bulk failure — the batch never ran.
    2. Any provider-side exception from ``embed_batch`` — auth, HTTP
       client errors, provider quota, a batch-size cap. Re-raises as
       before; now also counted as a bulk failure.
    """
    provider_name = _resolve_provider_name(tenant_config)
    # Provider construction and embed dispatch are wrapped in *separate*
    # try/excepts on purpose. Provider implementations (notably
    # ``OpenAIEmbeddingProvider._postprocess``) raise ``ValueError`` at
    # runtime — e.g. when the model returns fewer dimensions than
    # ``OPENAI_EMBEDDING_TRUNCATE_TO_DIM``. That is NOT a registry
    # misconfiguration; folding it into the misconfig branch would
    # incorrectly route runtime data errors through the misconfig path.
    #
    # On a registry ``ValueError``, this path logs but does NOT claim
    # the once-per-provider dedup gate (``_misconfiguration_logged``).
    # Bulk-call failures cascade into the per-item ``_reembed_memory``
    # fallback in ``memory_service``, which calls ``get_embedding`` →
    # ``_resolve_provider_or_degrade`` → that path owns the dedup gate
    # (and logs once with full context). If we `.add()` here, the per-
    # item fallback's first ERROR log gets silenced as a duplicate,
    # losing the more useful single-row attribution.
    try:
        provider = get_embedding_provider(provider_name, tenant_config)
    except ValueError:
        logger.error(
            "Bulk embedding: provider misconfiguration",
            exc_info=True,
        )
        # Deliberately NOT record_bulk_failure: the bulk call never ran,
        # so there is no batch to attribute a bulk-cap-style fault to, and
        # this branch already logs unconditionally. Scoped by NAME — there is
        # no provider object to take an identity from.
        await _stats_for(_stats_scope(None, provider_name), _stats_label(None, provider_name)).record_failure()
        raise
    scope = _stats_scope(provider, provider_name)
    label = _stats_label(provider, provider_name)
    # ``BaseException``, not ``Exception``, and deliberately so. A caller that
    # passes no ``budget_s`` still enforces its deadline by CANCELLING us, so
    # what arrives here is ``CancelledError`` — a ``BaseException``. Under
    # ``except Exception`` the stats update was skipped for that entire class,
    # which is the slow-provider outage the bulk streak exists to name.
    #
    # ``budget_s`` narrows that window rather than closing it: the inner cap
    # below turns the common case into a local ``TimeoutError``, but a caller
    # can still cancel us for its own reasons, and the margin can still be
    # overshot by a single syscall. The broad catch stays.
    #
    # Safe for real shutdown cancellation: the ``raise`` is unconditional, so
    # the cancellation always propagates. ``record_failure`` acquires an
    # uncontended ``asyncio.Lock`` — every critical section in
    # ``_EmbeddingStats`` is await-free, so the acquire takes its fast path and
    # never yields, and there is no suspension point at which a pending
    # cancellation could be re-delivered. If one ever were, we would land back
    # on today's behaviour (stat missed, cancellation still propagating), never
    # worse.
    #
    # Accepted cost: a cancellation that is NOT a deadline — a client
    # disconnect, process shutdown — also counts as a bulk failure. Bounded on
    # both sides: three consecutive are needed to report, the next bulk success
    # clears the streak, and on the shutdown path the counter dies with the
    # process anyway.
    try:
        if budget_s is None:
            result = await call_embedding_gated(
                lambda: provider.embed_batch(texts), background=background
            )
        else:
            # Floored rather than skipped: a budget at or under the margin is
            # a misconfiguration, and the honest response is to fail fast HERE
            # with an attributable TimeoutError instead of silently reverting
            # to the unbounded path the margin exists to replace.
            async with asyncio.timeout(max(0.1, budget_s - EMBEDDING_BUDGET_MARGIN_S)):
                result = await call_embedding_gated(
                    lambda: provider.embed_batch(texts), background=background
                )
    except (EmbeddingGateTimeout, EmbeddingBackendBusy):
        # Excluded from the bulk streak for the same reason the retry path
        # excludes both: the streak drives "Embedding service degraded
        # [<backend>]", and neither our own queue nor a backend correctly
        # shedding load is evidence about that backend's health. Still raised —
        # the caller has no vector either way, and raising is what routes the
        # batch to the durable per-item path described above.
        raise
    except BaseException:
        await _stats_for(scope, label).record_failure(bulk_batch_size=len(texts))
        raise
    await _stats_for(scope, label).record_success(bulk=True)
    return result


async def _run_with_retry(
    make_call: Callable[[], Awaitable[list[float]]],
    context: str,
    stats: _EmbeddingStats,
    *,
    background: bool,
) -> list[float] | None:
    """Execute *make_call* under the shared retry / stats / logging policy.

    *make_call* is invoked once per attempt, so it should construct a
    fresh coroutine each time it runs (coroutines aren't reusable across
    awaits). Returns ``None`` after the attempt budget is exhausted —
    callers degrade gracefully (write path persists ``embedding=NULL``;
    search path raises 503 upstream).

    *context* is a short human-readable label (``"Embedding"``,
    ``"Query embedding"``) interpolated into the per-attempt warning
    and the terminal error log so failures are attributable.

    *background* is forwarded to :func:`call_embedding_gated` — see
    ``EMBEDDING_INTERACTIVE_RESERVED_SLOTS``. It is per-ATTEMPT, like the slot
    itself, so a retrying background call re-queues behind the background
    budget instead of escalating into the reserved slice.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, EMBEDDING_RETRY_ATTEMPTS + 1):
        try:
            result = await call_embedding_gated(make_call, background=background)
            await stats.record_success()
            return result
        # Saturation is not failure, and retrying it is actively harmful:
        # each attempt queues another waiter for another full
        # ``EMBEDDING_GATE_TIMEOUT_SECONDS``, so the retry adds load to the
        # backlog it is reacting to while the caller waits
        # attempts x timeout + backoff to be told what the first attempt
        # already knew. Return immediately instead, on the FIRST timeout.
        #
        # Nothing is lost by not retrying: the provider never saw the call,
        # so there is no partial work to reconcile, and the degradation
        # contract is unchanged — ``None`` still means "no vector", which the
        # write path persists as NULL for the backfill sweep and the search
        # path turns into a 503. What changes is that it arrives in ~5s
        # instead of ~11s, and without a second slot-wait.
        #
        # The timeout itself is deliberately NOT counted via
        # ``record_failure``: that streak drives the "Embedding service
        # degraded [<backend>]" ERROR, and a queue we imposed on ourselves is
        # not evidence the backend is unhealthy. The gate's own warning is the
        # signal for this condition, and it names the caps.
        #
        # But an EARLIER attempt in this same call may have failed for a real
        # reason, and that evidence must survive. ``last_exc`` is set only by
        # the provider-failure branch below, so it is exactly the predicate
        # for "the backend already misbehaved this call"; without recording
        # it here, returning early would discard it, because
        # ``record_failure`` is otherwise only reached by exhausting the loop.
        # That mixed case is not exotic — a provider both erroring and slow
        # enough to saturate the gate is what an outage looks like, which is
        # precisely when the degraded signal must not go quiet.
        except EmbeddingGateTimeout:
            logger.warning(
                "%s gave up at the concurrency gate on attempt %d/%d — not "
                "retrying; retrying saturation deepens it%s",
                context,
                attempt,
                EMBEDDING_RETRY_ATTEMPTS,
                " (an earlier attempt failed for a provider reason; counting that)"
                if last_exc is not None
                else "",
            )
            if last_exc is not None:
                await stats.record_failure()
            return None
        # A 429 gets the identical treatment, for the identical reason one
        # layer out: the backend told us it is full, so a retry adds load
        # to the thing that is full. The only difference is that this one
        # DID reach the backend, so it cost a round trip — which is an
        # argument for retrying it less, not more.
        #
        # Not counted against the degraded streak either: that streak
        # drives "Embedding service degraded [<backend>]", and a backend
        # correctly shedding load is healthy. Counting it would page an
        # operator toward a service that is behaving exactly as intended,
        # which is what the 2026-08-18 window looked like from the logs.
        # ``last_exc`` carries the same meaning as above — a genuine
        # earlier failure in this call must still be recorded.
        #
        # The message says what THIS layer did and stops there. It must not
        # claim the work is deferred: whether a ``None`` becomes durable
        # work is the caller's property, not ours. A write's ``None``
        # persists as ``embedding=NULL`` for the backfill sweep and a
        # batch's fans out to EMBED_REQUESTED, but a query embed has no
        # queue behind it at all — the search just fails with a 503. An
        # operator reading "the work defers instead" during a search
        # outage would be told recovery was in hand when it was not, in
        # exactly the incident this classification exists to make legible.
        except EmbeddingBackendBusy:
            logger.warning(
                "%s stopped at a busy backend on attempt %d/%d — not retrying;"
                " a refusal is capacity, and retrying it deepens the shortage%s",
                context,
                attempt,
                EMBEDDING_RETRY_ATTEMPTS,
                " (an earlier attempt failed for a provider reason; counting that)"
                if last_exc is not None
                else "",
            )
            if last_exc is not None:
                await stats.record_failure()
            return None
        # Intentionally broad: must catch all provider-specific errors during retry.
        except Exception as exc:
            # Capture so the terminal log below can attach the stack trace.
            # ``exc_info=True`` outside an except block reads
            # ``sys.exc_info()`` which has been cleared by the time the
            # loop exits, so we have to bind the exception explicitly.
            last_exc = exc
            logger.warning(
                "%s attempt %d/%d failed",
                context,
                attempt,
                EMBEDDING_RETRY_ATTEMPTS,
                exc_info=True,
            )
            if attempt < EMBEDDING_RETRY_ATTEMPTS:
                await asyncio.sleep(EMBEDDING_RETRY_DELAY_S * attempt)
    await stats.record_failure()
    logger.error(
        "%s failed after %d attempts, returning None",
        context,
        EMBEDDING_RETRY_ATTEMPTS,
        exc_info=last_exc,
    )
    return None


async def _resolve_provider_or_degrade(
    tenant_config: object | None,
    context: str,
) -> object | None:
    """Resolve the embedding provider, mapping a misconfiguration
    ``ValueError`` from the registry to the same ``None`` degradation
    contract the rest of this module documents.

    ``get_embedding_provider`` raises ``ValueError`` on env-var misconfig
    (``base_url`` ⊕ ``send_dimensions`` mismatch, invalid
    ``OPENAI_EMBEDDING_TRUNCATE_TO_DIM``, etc.). Without this guard the
    error would propagate out of ``get_embedding`` / ``get_query_embedding``
    and break callers that rely on "returns None on failure" — write
    paths persist rows with ``embedding=NULL`` for later backfill, search
    paths typically translate None → 503. Logging once at error level
    keeps the misconfiguration visible without making the request handler
    crash.
    """
    provider_name = _resolve_provider_name(tenant_config)
    try:
        return get_embedding_provider(provider_name, tenant_config)
    except ValueError:
        # Dedup: once-per-provider-name so a misconfigured deployment
        # gets a single ERROR at first request rather than one per
        # request. Failure stats still increment on every call, so
        # the degraded-provider trip-wire in ``_EmbeddingStats`` still
        # fires correctly under sustained misconfiguration.
        if provider_name not in _misconfiguration_logged:
            _misconfiguration_logged.add(provider_name)
            logger.error(
                "%s: provider misconfiguration (will not repeat); returning None",
                context,
                exc_info=True,
            )
        await _stats_for(_stats_scope(None, provider_name), _stats_label(None, provider_name)).record_failure()
        return None


async def get_embedding(
    text: str,
    tenant_config: object | None = None,
    *,
    background: bool,
) -> list[float] | None:
    """Generate an embedding with retry. Returns ``None`` on exhausted retries.

    Caller-friendly degradation: a transient OpenAI/Vertex hiccup
    returns ``None`` rather than raising, so write paths can persist
    rows with ``embedding=NULL`` and let the async-embed worker backfill.
    The same ``None`` is returned on a registry-level
    ``ValueError`` (env-var misconfiguration) — see
    :func:`_resolve_provider_or_degrade`.

    This is the document/ingest path. For search-side queries that should
    pass through an instruction-aware encoder (Qwen3-Embedding, e5-instruct,
    etc.), use :func:`get_query_embedding` instead.

    *background* is REQUIRED — on this function and on
    :func:`get_embeddings_batch` — and deliberately has no default. The
    classification cannot be inferred here: ``common/embedding`` has no
    dependency on service config, so it cannot see ``deployment_mode``,
    ``write_mode``, or whether an HTTP client is blocked on the response.
    Only the call site knows. A default made the wrong answer the one you got
    by forgetting, and four review passes over this change each found a
    different call site that had silently inherited it — document search, the
    inline write path, document/skill write, and the bulk/auto-chunk batch
    paths. Omitting it is now a ``TypeError``.

    Pass ``background=False`` when a caller is waiting on the result. Pass
    ``background=True`` for deferred work nobody is waiting on. See
    ``EMBEDDING_INTERACTIVE_RESERVED_SLOTS``.
    """
    # Blank text is a CALLER problem, not a backend one, and the backend
    # says so: TEI answers ``413 Input validation error: `inputs` cannot be
    # empty``. Prod took 274 of those in one week — 272 inside the
    # 2026-08-18 17:00-18:59 incident — because nothing checked, each one
    # was retried (the error is deterministic, so the retry could only fail
    # again), and the ``None`` that came out was reported to users as
    # "Embedding service unavailable" while the backend was serving
    # normally at ~7 ms.
    #
    # Returning ``None`` keeps the documented contract — callers already
    # handle it — but now it costs zero backend requests instead of two,
    # and the log names the real cause instead of blaming the provider.
    if is_blank_text(text):
        logger.warning(
            "Embedding skipped: blank text, nothing to encode (no request sent)"
        )
        return None

    provider_name = _resolve_provider_name(tenant_config)
    provider = await _resolve_provider_or_degrade(tenant_config, "Embedding")
    if provider is None:
        return None
    return await _run_with_retry(
        lambda: provider.embed(text),
        "Embedding",
        _stats_for(
            _stats_scope(provider, provider_name),
            _stats_label(provider, provider_name),
        ),
        background=background,
    )


async def get_query_embedding(
    text: str,
    tenant_config: object | None = None,
    instruction: str | None = None,
) -> list[float] | None:
    """Generate a query-side embedding (instruction-aware, asymmetric).

    For instruction-aware models (Qwen3-Embedding, e5-instruct, KaLM), the
    query encoder expects a task-description prefix that documents do not
    receive. Symmetric providers (OpenAI ``text-embedding-3-small``,
    ``bge-m3``, ``gte-en-v1.5``, ``Fake``, ``Local``, ``Vertex``) do not
    implement :class:`~common.embedding.protocols.InstructionAwareEmbedder`
    — the call site below detects that and falls back to the symmetric
    :meth:`embed` path, matching :func:`get_embedding`.

    Same retry / degradation semantics as :func:`get_embedding`: returns
    ``None`` after exhausted retries rather than raising, so search routes
    can degrade gracefully (typically by raising 503 to the caller). The
    same ``None`` is returned on a registry-level ``ValueError`` (env-var
    misconfiguration).
    """
    # Same guard as :func:`get_embedding`, and this is the path that was
    # actually hit: the 2026-08-18 tracebacks are all ``embed_query``. A
    # blank search query is the easiest blank text to produce.
    if is_blank_text(text):
        logger.warning(
            "Query embedding skipped: blank text, nothing to encode (no request sent)"
        )
        return None

    provider_name = _resolve_provider_name(tenant_config)
    provider = await _resolve_provider_or_degrade(tenant_config, "Query embedding")
    if provider is None:
        return None

    # ``InstructionAwareEmbedder`` is an optional ``@runtime_checkable``
    # Protocol declared in ``common.embedding.protocols`` for exactly
    # this dispatch. Only providers backed by an instruction-aware
    # model (e.g. ``OpenAIEmbeddingProvider`` pointed at
    # Qwen3-Embedding) conform. ``Fake`` / ``Local`` / ``Vertex`` do
    # not implement ``embed_query`` and the isinstance check returns
    # False — they fall through to :meth:`embed` and silently ignore
    # *instruction*.
    is_instruction_aware = isinstance(provider, InstructionAwareEmbedder)

    async def _call() -> list[float]:
        # Inner ``def`` rather than a ``lambda`` so the closure captures
        # *provider* / *text* / *instruction* with explicit ``await``
        # ergonomics; ruff E731 disapproves of ``make_call = lambda:``.
        if is_instruction_aware:
            return await provider.embed_query(text, instruction)
        return await provider.embed(text)

    return await _run_with_retry(
        _call,
        "Query embedding",
        _stats_for(
            _stats_scope(provider, provider_name),
            _stats_label(provider, provider_name),
        ),
        # Search is always interactive — a user is waiting on the results.
        background=False,
    )
