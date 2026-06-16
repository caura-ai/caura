"""Transient-error retry policy for core-storage-api HTTP clients.

Shared by core-api's ``CoreStorageClient`` and core-worker's storage
client so the two cannot silently diverge on what is safe to retry.

History (load-bearing for the policy split):

* F5 — Cloud Run logs over 7 days showed a 31% silent-failure rate on
  ``process_entity_extraction`` on ``staging-memclaw-core-api``. Every
  failure traced to ``httpx.ConnectTimeout`` reaching core-storage-api
  (cold starts / autoscaling). Retry landed for idempotent methods
  (GET, PATCH, DELETE) only.
* 2026-06-11 prod incident — ``find_similar_candidates`` (contradiction
  detection, 42 failures) and ``create_audit_logs_bulk`` (audit events
  dropped) both died on first-attempt ``ConnectTimeout`` behind the VPC
  connector because POSTs had no retry at all. Connection-phase retry
  for non-idempotent methods landed in response (caura-memclaw#333).
* 2026-06-16 prod — a steady trickle of ``httpx.ConnectTimeout`` still
  reached core-api on the Pub/Sub enrichment, entity-extraction,
  contradiction and audit-flush paths (storage-api cold starts /
  instance recycles). 3 attempts (~0.6s of backoff) is too short to
  ride out a cold start, so the connect-phase path now retries more
  times — see ``CONNECT_PHASE_MAX_ATTEMPTS``. This complements, not
  replaces, keeping storage-api warm (Cloud Run min-instances).

Retry policy:
 - Idempotent methods: max 3 attempts (1 initial + 2 retries), retry
   ``RETRYABLE_EXCEPTIONS`` plus ``RETRYABLE_STATUS_CODES``
   (use :func:`with_retry` defaults). Kept at 3 so a genuine 5xx
   storm isn't amplified by extra load against a struggling server.
 - Non-idempotent methods: ``CONNECT_PHASE_EXCEPTIONS`` ONLY
   (use :func:`with_connect_phase_retry`), max ``CONNECT_PHASE_MAX_ATTEMPTS``
   attempts. ConnectTimeout / ConnectError / PoolTimeout are all raised
   before a single request byte is written, so a retry cannot
   double-insert — and against an instance that isn't accepting yet
   they add no server load, so retrying more times just rides out the
   cold start. ReadTimeout and 5xx are NOT retried there — the request
   reached storage and may have committed; safe retry needs
   storage-side idempotency keys.
 - Exponential backoff with jitter, capped at ``RETRY_BACKOFF_MAX_S``.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

RETRY_MAX_ATTEMPTS = 3
RETRY_BACKOFF_BASE_S = 0.2
# Cap any single backoff sleep so a higher attempt count can't grow the delay
# unboundedly (matters once CONNECT_PHASE_MAX_ATTEMPTS pushes past 3 attempts).
RETRY_BACKOFF_MAX_S = 2.0
# Connection-phase failures (CONNECT_PHASE_EXCEPTIONS) are raised before the
# request is sent and against an instance that isn't accepting yet, so retrying
# them adds no load to a healthy server — it just rides out a Cloud Run cold
# start / instance recycle. We retry them MORE times than the idempotent default
# so a transient storage blip doesn't surface as a handler failure (and, on the
# Pub/Sub path, a nack → immediate-redelivery storm).
CONNECT_PHASE_MAX_ATTEMPTS = 5
RETRYABLE_EXCEPTIONS = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.PoolTimeout,
    # ConnectError covers refused / DNS-not-yet-resolved / route-down —
    # all transient during Cloud Run autoscaling and storage-api
    # restarts. Local chaos test (docker network disconnect) showed
    # httpx raises ConnectError("Name or service not known"), not
    # ConnectTimeout, when the upstream is temporarily unreachable.
    httpx.ConnectError,
)
# Failures raised while establishing/acquiring a connection — the
# request body was never transmitted, so retrying is safe even for
# non-idempotent methods. ReadTimeout is deliberately absent.
CONNECT_PHASE_EXCEPTIONS = (
    httpx.ConnectTimeout,
    httpx.ConnectError,
    httpx.PoolTimeout,
)
RETRYABLE_STATUS_CODES = frozenset({502, 503, 504})
NO_RETRYABLE_STATUSES: frozenset[int] = frozenset()


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff for a 1-based ``attempt``, with ±10% jitter and a
    hard ceiling of ``RETRY_BACKOFF_MAX_S``. The cap is applied AFTER jitter so
    a single sleep never exceeds it (jitter on the already-capped value would
    push the real ceiling to RETRY_BACKOFF_MAX_S * 1.1)."""
    delay = RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1))
    return min(delay * (1.0 + random.uniform(-0.1, 0.1)), RETRY_BACKOFF_MAX_S)


async def with_retry(
    do_request: Callable[[], Awaitable[httpx.Response]],
    *,
    label: str,
    retryable_exceptions: tuple[type[Exception], ...] = RETRYABLE_EXCEPTIONS,
    retryable_statuses: frozenset[int] = RETRYABLE_STATUS_CODES,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
) -> httpx.Response:
    """Wrap a single HTTP call with retry on transient errors.

    Defaults match the idempotent-method policy: retry on
    ``ConnectTimeout`` / ``ReadTimeout`` / ``PoolTimeout`` (transient
    connection issues) and on 5xx responses in
    ``RETRYABLE_STATUS_CODES`` (transient server-side). 4xx (including
    404) and other 2xx/3xx responses are returned immediately —
    retrying won't change a client error. Non-idempotent callers go
    through :func:`with_connect_phase_retry` to retry only failures
    where the request was provably never sent.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = await do_request()
        except retryable_exceptions as e:
            last_exc = e
            if attempt < max_attempts:
                delay = _backoff_delay(attempt)
                logger.warning(
                    "storage_client.%s: %s on attempt %d/%d, retrying in %.2fs",
                    label,
                    type(e).__name__,
                    attempt,
                    max_attempts,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.warning(
                "storage_client.%s: %s on final attempt %d/%d, giving up",
                label,
                type(e).__name__,
                attempt,
                max_attempts,
            )
            raise
        if resp.status_code in retryable_statuses and attempt < max_attempts:
            delay = _backoff_delay(attempt)
            logger.warning(
                "storage_client.%s: HTTP %d on attempt %d/%d, retrying in %.2fs",
                label,
                resp.status_code,
                attempt,
                max_attempts,
                delay,
            )
            await asyncio.sleep(delay)
            continue
        if resp.status_code in retryable_statuses:
            # Final attempt still returned a retryable status — mirror the
            # exception path's "giving up" signal so a 3x-502 incident is
            # visible as exhausted retries, not just a bare HTTPStatusError
            # from the caller's raise_for_status().
            logger.warning(
                "storage_client.%s: HTTP %d on final attempt %d/%d, giving up",
                label,
                resp.status_code,
                attempt,
                max_attempts,
            )
        return resp
    # Unreachable — the loop either returns a response or raises.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("storage_client retry loop exited without response or exception")


async def with_connect_phase_retry(
    do_request: Callable[[], Awaitable[httpx.Response]],
    *,
    label: str,
) -> httpx.Response:
    """Retry policy for non-idempotent calls, encoded in one place.

    Connection-phase failures only — the request was provably never
    sent, so a retry cannot double-insert. No status-based retries.
    Uses ``CONNECT_PHASE_MAX_ATTEMPTS`` (> the idempotent default): these
    failures add no load to a healthy server, so retrying more times just
    rides out a cold start / instance recycle.
    """
    return await with_retry(
        do_request,
        label=label,
        retryable_exceptions=CONNECT_PHASE_EXCEPTIONS,
        retryable_statuses=NO_RETRYABLE_STATUSES,
        max_attempts=CONNECT_PHASE_MAX_ATTEMPTS,
    )
