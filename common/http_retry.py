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

Retry policy:
 - Max 3 attempts (1 initial + 2 retries)
 - Exponential backoff with jitter: ~0.2s, ~0.4s
 - Worst-case added latency: < 1s
 - Idempotent HTTP methods retry ``RETRYABLE_EXCEPTIONS`` plus
   ``RETRYABLE_STATUS_CODES`` (use :func:`with_retry` defaults).
 - Non-idempotent methods retry ``CONNECT_PHASE_EXCEPTIONS`` ONLY
   (use :func:`with_connect_phase_retry`): ConnectTimeout /
   ConnectError / PoolTimeout are all raised before a single request
   byte is written, so a retry cannot double-insert. ReadTimeout and
   5xx are NOT retried there — the request reached storage and may
   have committed; safe retry needs storage-side idempotency keys.
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


async def with_retry(
    do_request: Callable[[], Awaitable[httpx.Response]],
    *,
    label: str,
    retryable_exceptions: tuple[type[Exception], ...] = RETRYABLE_EXCEPTIONS,
    retryable_statuses: frozenset[int] = RETRYABLE_STATUS_CODES,
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
    for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
        try:
            resp = await do_request()
        except retryable_exceptions as e:
            last_exc = e
            if attempt < RETRY_MAX_ATTEMPTS:
                delay = RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1))
                delay *= 1.0 + random.uniform(-0.1, 0.1)  # ±10% jitter
                logger.warning(
                    "storage_client.%s: %s on attempt %d/%d, retrying in %.2fs",
                    label,
                    type(e).__name__,
                    attempt,
                    RETRY_MAX_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            logger.warning(
                "storage_client.%s: %s on final attempt %d/%d, giving up",
                label,
                type(e).__name__,
                attempt,
                RETRY_MAX_ATTEMPTS,
            )
            raise
        if resp.status_code in retryable_statuses and attempt < RETRY_MAX_ATTEMPTS:
            delay = RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1))
            delay *= 1.0 + random.uniform(-0.1, 0.1)
            logger.warning(
                "storage_client.%s: HTTP %d on attempt %d/%d, retrying in %.2fs",
                label,
                resp.status_code,
                attempt,
                RETRY_MAX_ATTEMPTS,
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
                RETRY_MAX_ATTEMPTS,
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
    """
    return await with_retry(
        do_request,
        label=label,
        retryable_exceptions=CONNECT_PHASE_EXCEPTIONS,
        retryable_statuses=NO_RETRYABLE_STATUSES,
    )
