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

from common.embedding._registry import get_embedding_provider
from common.embedding.constants import (
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

    def __init__(self) -> None:
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
        process-wide ``_stats``, so a service that also serves search
        traffic records a success per query embed, which resets
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

        LIMITATION worth knowing: the streak is process-wide and NOT keyed
        on the provider, so in a multi-tenant process one tenant's healthy
        bulk success clears a different tenant's broken backend — the same
        masking, one level down. ``common/ranking``'s ``_permanent_scope``
        is the per-backend answer; applying it here means keying all of
        ``_stats``, which is a wider change than this. Do not read the
        streak as per-backend.
        """
        async with self._lock:
            self.failures += 1
            self.consecutive_failures += 1
            self.last_failure_time = time.monotonic()
            if bulk_batch_size is not None:
                self.consecutive_bulk_failures += 1
                if self._is_report_point(self.consecutive_bulk_failures):
                    logger.warning(
                        "Bulk embedding failing: %d consecutive bulk call(s) "
                        "failed (requested batch=%d), cascading to the "
                        "per-item fallback. Embeddings may still be correct; "
                        "batching is not. The provider already splits a "
                        "request to its own backend's cap, so a batch-size "
                        "rejection is unlikely — look at auth, network, "
                        "quota and the callers' timeouts.",
                        self.consecutive_bulk_failures,
                        bulk_batch_size,
                    )
            if self._is_report_point(self.consecutive_failures):
                logger.error(
                    "Embedding service degraded: %d consecutive failures (total: %d/%d)",
                    self.consecutive_failures,
                    self.failures,
                    self.failures + self.successes,
                )


_stats = _EmbeddingStats()

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


async def _call_gated[T](make_call: Callable[[], Awaitable[T]]) -> T:
    """Run *make_call* holding a concurrency slot.

    The slot is acquired per ATTEMPT, not per retry sequence, so a
    backing-off retry never squats on a slot another caller could use.

    A slot that doesn't free within ``EMBEDDING_GATE_TIMEOUT_SECONDS``
    raises ``TimeoutError``, which callers already treat as a provider
    failure — the pre-existing degradation path — rather than letting
    waiters accumulate unboundedly and stall the write path.
    """
    gate = _concurrency_gate()
    # Log saturation explicitly. During the 2026-07-27 incident the backend
    # reported 3.5 ms inference while callers timed out, and nothing said
    # where the time went. These two messages separate "queued behind our
    # own cap" from "backend slow" for the next one.
    if gate.locked():
        logger.debug(
            "Embedding concurrency gate saturated (cap=%d); queueing",
            EMBEDDING_MAX_CONCURRENCY,
        )
    # ``asyncio.timeout`` rather than ``wait_for``: it matches the sibling
    # gates (``per_tenant_concurrency``), and ``wait_for`` is reached via the
    # shared ``asyncio`` module object, so tests that patch
    # ``<their_module>.asyncio.wait_for`` to spy on their OWN ceiling would
    # instead capture this gate's timeout and assert against the wrong value.
    try:
        async with asyncio.timeout(EMBEDDING_GATE_TIMEOUT_SECONDS):
            await gate.acquire()
    except TimeoutError:
        logger.warning(
            "Embedding concurrency gate timeout after %.1fs (cap=%d) — "
            "degrading as provider failure; backend may be undersized",
            EMBEDDING_GATE_TIMEOUT_SECONDS,
            EMBEDDING_MAX_CONCURRENCY,
        )
        raise
    try:
        return await make_call()
    finally:
        gate.release()


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
) -> list[list[float]]:
    """Generate embeddings for multiple texts in a single API call.

    Raises on any provider-side error. Bulk callers
    (``memory_service.create_memories_bulk``,
    ``_reembed_batch_via_provider``) already wrap this in
    ``try: ... except Exception:`` and fall back to per-item retries,
    so any exception type is acceptable here — what matters is that
    the failure stats counter increments so the registry-level
    degraded-provider trip-wire fires consistently with the single-embed
    paths (``get_embedding`` / ``get_query_embedding``).

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
        # this branch already logs unconditionally.
        await _stats.record_failure()
        raise
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
            result = await _call_gated(lambda: provider.embed_batch(texts))
        else:
            # Floored rather than skipped: a budget at or under the margin is
            # a misconfiguration, and the honest response is to fail fast HERE
            # with an attributable TimeoutError instead of silently reverting
            # to the unbounded path the margin exists to replace.
            async with asyncio.timeout(max(0.1, budget_s - EMBEDDING_BUDGET_MARGIN_S)):
                result = await _call_gated(lambda: provider.embed_batch(texts))
    except BaseException:
        await _stats.record_failure(bulk_batch_size=len(texts))
        raise
    await _stats.record_success(bulk=True)
    return result


async def _run_with_retry(
    make_call: Callable[[], Awaitable[list[float]]],
    context: str,
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
    """
    last_exc: BaseException | None = None
    for attempt in range(1, EMBEDDING_RETRY_ATTEMPTS + 1):
        try:
            result = await _call_gated(make_call)
            await _stats.record_success()
            return result
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
    await _stats.record_failure()
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
        await _stats.record_failure()
        return None


async def get_embedding(
    text: str, tenant_config: object | None = None
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
    """
    provider = await _resolve_provider_or_degrade(tenant_config, "Embedding")
    if provider is None:
        return None
    return await _run_with_retry(lambda: provider.embed(text), "Embedding")


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

    return await _run_with_retry(_call, "Query embedding")
