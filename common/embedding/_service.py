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
from common.embedding.constants import EMBEDDING_RETRY_ATTEMPTS, EMBEDDING_RETRY_DELAY_S
from common.embedding.protocols import InstructionAwareEmbedder

logger = logging.getLogger(__name__)


class _EmbeddingStats:
    """Track failure rate to surface a degraded-provider signal in logs.

    Three consecutive failures fire a single ERROR log (per cycle) so a
    sustained provider outage shows up loudly without spamming once-per-
    request. Reset on the next success.
    """

    def __init__(self) -> None:
        self.failures = 0
        self.successes = 0
        self.last_failure_time = 0.0
        self.consecutive_failures = 0
        self._lock = asyncio.Lock()

    async def record_success(self) -> None:
        async with self._lock:
            self.successes += 1
            self.consecutive_failures = 0

    async def record_failure(self) -> None:
        async with self._lock:
            self.failures += 1
            self.consecutive_failures += 1
            self.last_failure_time = time.monotonic()
            # Fire on first detection (failure 3) and then every 10
            # failures thereafter (13, 23, 33, …). Loud enough to alert
            # on a fresh outage, quiet enough not to spam during a
            # sustained provider degradation. The next success resets
            # the counter, so a subsequent streak re-fires from 3.
            if (
                self.consecutive_failures >= 3
                and (self.consecutive_failures - 3) % 10 == 0
            ):
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


def _resolve_provider_name(tenant_config: object | None) -> str:
    """Tenant override first, else ``EMBEDDING_PROVIDER`` env, else ``"fake"``."""
    if tenant_config is not None:
        name = getattr(tenant_config, "embedding_provider", None)
        if name:
            return name
    return os.environ.get("EMBEDDING_PROVIDER", "fake")


async def get_embeddings_batch(
    texts: list[str], tenant_config: object | None = None
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

    Two error shapes are explicitly accounted for:

    1. ``ValueError`` from ``get_embedding_provider`` — env-var
       misconfiguration (e.g. ``OPENAI_EMBEDDING_BASE_URL`` ⊕
       ``SEND_DIMENSIONS`` mismatch). Used to propagate as an unhandled
       exception that bulk callers caught generically but bypassed
       ``_stats.record_failure``, so the trip-wire never fired under
       sustained misconfig. Now records a failure and re-raises.
    2. Any provider-side exception from ``embed_batch`` — auth, HTTP
       client errors, Vertex quota, etc. Same record_failure + re-raise
       contract as before.
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
        await _stats.record_failure()
        raise
    try:
        result = await provider.embed_batch(texts)
    except Exception:
        await _stats.record_failure()
        raise
    await _stats.record_success()
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
            result = await make_call()
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
