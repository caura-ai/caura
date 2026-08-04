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
from common.ranking.errors import PermanentRankError
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

# Same log-once posture for permanent provider faults, keyed on
# ``PermanentRankError.key`` (a stable backend+condition string, NOT the
# message — that embeds a response body and varies per request). A permanent
# fault recurs on every search by definition, so logging it in full each time
# would make ERROR volume a function of traffic rather than of the fault.
# ``_stats.record_failure()`` still fires every time, so the degraded
# trip-wire keeps counting occurrences.
#
# Entries are cleared per BACKEND on that backend's next success, never
# wholesale: one process serves many tenants and holds a rank provider per
# ``(base_url, api_key, model)``, so a healthy tenant's success must not
# re-arm — or suppress — a different tenant's broken sidecar. Clearing
# globally would put ERROR volume right back on a traffic curve, this time
# driven by unrelated tenants.
_permanent_logged: set[str] = set()

# Bound it. Each entry is one (backend, condition) pair, so the steady state is
# tiny — but a provider whose scope is per-instance means a long-lived process
# that rotates tenant rank config accumulates a dead entry per retired backend,
# the same leak ``_registry.py`` caps its ranker cache at 32 to avoid. On
# overflow we drop the whole set rather than track recency: the only cost is
# that a still-broken backend may report once more than strictly needed, which
# is the safe direction to fail.
_PERMANENT_LOGGED_MAX = 256


def _permanent_scope(provider: object) -> str:
    """Dedup namespace for ``provider`` — see ``PermanentRankError.key``.

    Falls back to the provider name for providers that don't declare a scope
    (``noop``/``fake`` never raise permanently, and a third-party provider
    without one still gets coarse per-name dedup rather than none).
    """
    scope = getattr(provider, "dedup_scope", None)
    if scope:
        return str(scope)
    return str(getattr(provider, "provider_name", "unknown"))


def _clear_permanent_for(provider: object) -> None:
    """Re-arm the ERROR for one backend after it succeeds again."""
    prefix = f"{_permanent_scope(provider)}|"
    _permanent_logged.difference_update(
        {key for key in _permanent_logged if key.startswith(prefix)}
    )


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
            # Re-arm THIS backend's permanent-fault ERROR: if it was fixed and
            # later regresses, the next fault reports in full rather than
            # staying silent at DEBUG forever. Scoped, so a healthy tenant
            # can't re-arm a still-broken one.
            _clear_permanent_for(provider)
            return scores
        except TimeoutError:
            logger.warning(
                "Ranking attempt %d/%d timed out after %.3fs",
                attempt,
                RANK_RETRY_ATTEMPTS,
                RANK_TIMEOUT_SECONDS,
            )
        # A configuration-class fault (see common/ranking/errors.py): it fails
        # identically next attempt, so stop instead of spending the turn's
        # latency to reach the same answer. ERROR, not warning, because the
        # search still succeeds on first-stage order — this log line is the
        # only symptom the fault has. Deduped per condition; see
        # ``_permanent_logged``.
        except PermanentRankError as exc:
            await _stats.record_failure()
            if exc.key in _permanent_logged:
                logger.debug("Ranking permanently failing (already reported): %s", exc)
            else:
                if len(_permanent_logged) >= _PERMANENT_LOGGED_MAX:
                    _permanent_logged.clear()
                _permanent_logged.add(exc.key)
                logger.error(
                    "Ranking failed permanently, not retrying; keeping "
                    "first-stage order. This recurs until the configuration "
                    "changes; further occurrences log at DEBUG until the next "
                    "success. %s",
                    exc,
                )
            return None
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
