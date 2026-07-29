"""Service-level ranking entrypoint with retry, timeout, and degrade-to-first-stage.

Mirrors ``common/embedding/_service.py``. Reads provider selection from a
tenant override (``tenant_config.rank_provider``) or the ``RANK_PROVIDER``
env, defaulting to ``noop``. The contract every caller relies on:

    get_ranking(...) -> list[float] | None

``None`` means "keep first-stage order" — returned on misconfig, timeout,
empty input, or exhausted retries. Recall must NEVER fail because rerank
did, so every failure path degrades rather than raises. This is the
embedding module's "persist NULL / fall back" posture applied to ranking.
"""

from __future__ import annotations

import asyncio
import logging
import os

from common.ranking._registry import get_rank_provider
from common.ranking.constants import (
    RANK_RETRY_ATTEMPTS,
    RANK_RETRY_DELAY_S,
    RANK_TIMEOUT_SECONDS,
)
from common.ranking.protocols import RankCandidate

logger = logging.getLogger(__name__)


class _RankStats:
    """Fire a single ERROR after N consecutive failures (degraded trip-wire).

    Mirrors ``_EmbeddingStats``: loud on a fresh outage, quiet during a
    sustained one, reset on the next success.
    """

    def __init__(self) -> None:
        self.failures = 0
        self.successes = 0
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
            if (
                self.consecutive_failures >= 3
                and (self.consecutive_failures - 3) % 10 == 0
            ):
                logger.error(
                    "Ranking service degraded: %d consecutive failures (total: %d/%d)",
                    self.consecutive_failures,
                    self.failures,
                    self.failures + self.successes,
                )


_stats = _RankStats()

# One-shot misconfiguration log dedup, keyed on provider name — a bad
# ``RANK_PROVIDER`` would otherwise log once per search.
_misconfiguration_logged: set[str] = set()


def _resolve_provider_name(tenant_config: object | None) -> str:
    """Tenant override first, else ``RANK_PROVIDER`` env, else ``"noop"``."""
    if tenant_config is not None:
        name = getattr(tenant_config, "rank_provider", None)
        if name:
            return name
    return os.environ.get("RANK_PROVIDER") or "noop"


def _resolve_provider_or_degrade(tenant_config: object | None):
    """Resolve the provider, mapping an unknown-name ``ValueError`` to ``None``.

    Returns ``None`` on misconfiguration (logged once per provider name),
    so the caller keeps first-stage order instead of crashing the search.
    """
    provider_name = _resolve_provider_name(tenant_config)
    try:
        return get_rank_provider(provider_name, tenant_config)
    except ValueError:
        if provider_name not in _misconfiguration_logged:
            _misconfiguration_logged.add(provider_name)
            logger.error(
                "Ranking: provider misconfiguration %r (will not repeat); "
                "keeping first-stage order",
                provider_name,
                exc_info=True,
            )
        return None


async def get_ranking(
    query: str,
    candidates: list[RankCandidate],
    tenant_config: object | None = None,
) -> list[float] | None:
    """Score ``candidates`` for ``query``; return one score each, input order.

    Returns ``None`` (caller keeps first-stage order) when the provider is
    misconfigured, there are no candidates, or the call times out / errors
    past the retry budget. Enforces a hard ``RANK_TIMEOUT_SECONDS`` deadline
    per attempt — recall is latency-critical, a slow rerank must not extend
    the turn.
    """
    if not candidates:
        return None
    provider = _resolve_provider_or_degrade(tenant_config)
    if provider is None:
        return None

    for attempt in range(1, RANK_RETRY_ATTEMPTS + 1):
        try:
            scores = await asyncio.wait_for(
                provider.rank(query, candidates), timeout=RANK_TIMEOUT_SECONDS
            )
            if len(scores) != len(candidates):
                # Contract violation — treat as a failure, degrade rather
                # than return a misaligned scores<->candidates mapping.
                raise ValueError(
                    f"ranker returned {len(scores)} scores for {len(candidates)} candidates"
                )
            await _stats.record_success()
            return scores
        except TimeoutError:
            logger.warning(
                "Ranking attempt %d/%d timed out after %.3fs",
                attempt,
                RANK_RETRY_ATTEMPTS,
                RANK_TIMEOUT_SECONDS,
            )
        # Intentionally broad: any provider error degrades to first-stage.
        except Exception:
            logger.warning(
                "Ranking attempt %d/%d failed",
                attempt,
                RANK_RETRY_ATTEMPTS,
                exc_info=True,
            )
        if attempt < RANK_RETRY_ATTEMPTS:
            await asyncio.sleep(RANK_RETRY_DELAY_S * attempt)

    await _stats.record_failure()
    logger.error(
        "Ranking failed after %d attempt(s); keeping first-stage order",
        RANK_RETRY_ATTEMPTS,
    )
    return None
