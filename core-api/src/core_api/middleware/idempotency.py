"""Idempotency-Key helper (CAURA-601).

Per-route opt-in: handlers call :func:`idempotency_for` after auth
has enforced the tenant, get back a :class:`IdempotencyGuard`, check
``guard.cached_replay`` for a cached response, and call
``guard.record(...)`` after running the work.

Not a middleware — slowapi + FastAPI already handle the request/response
pipeline, and per-handler wiring keeps the replay contract explicit at
the route level. The boilerplate is five lines per decorated handler.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import HTTPException, Request

from core_api.clients.storage_client import get_storage_client
from core_api.config import settings

logger = logging.getLogger(__name__)

IDEMPOTENCY_HEADER = "Idempotency-Key"
# Matches Stripe's limit. Guards the `Text` column against oversized
# values from buggy or adversarial clients before it reaches storage.
MAX_IDEMPOTENCY_KEY_LEN = 255

# Poll cadence + budget while waiting for a concurrent holder of the
# same key to finish. Total wait =
# ``_CLAIM_POLL_INTERVAL_SECONDS * _CLAIM_POLL_MAX_ATTEMPTS``. Long
# enough to absorb a typical write pipeline (~2-5s end-to-end) without
# holding the second request indefinitely; on timeout the caller gets
# 409 and can retry.
_CLAIM_POLL_INTERVAL_SECONDS = 0.5
_CLAIM_POLL_MAX_ATTEMPTS = 20

# Sentinels ``_poll_until_complete`` may return alongside the
# completed row dict and ``None`` (budget exhausted):
#
# - ``_POLL_VANISHED``: the row no longer exists (cleanup-job pruned
#   or pending TTL elapsed). Caller can tell the client to retry as
#   a fresh request.
# - ``_POLL_STORAGE_ERROR``: the storage call itself failed (down,
#   timeout, network). Caller should respond 503 — retrying into a
#   downed cluster wastes the client's retry budget.
_POLL_VANISHED = "vanished"
_POLL_STORAGE_ERROR = "storage_error"

# Body-metadata keys checked when the ``Idempotency-Key`` HTTP
# header is absent. ``Idempotency-Key`` (header) remains the
# canonical form per the IETF draft; the body fallback exists for
# SDK / load-test clients that can't easily set headers per request.
# ``idempotency_key`` first, then the legacy ``client_idempotency_key``
# alias the loadtest harness uses.
_IDEMPOTENCY_BODY_FIELDS = ("idempotency_key", "client_idempotency_key")


def idempotency_key_from_metadata(metadata: dict | None) -> str | None:
    """Extract the idempotency key from a request body's ``metadata``
    dict, if present. Returns ``None`` if metadata is missing/empty
    or no recognised key field is set.

    The header takes precedence — call this only as a fallback when
    the header is absent. Routes that wire idempotency wire it once
    via:

        key = idempotency_header or idempotency_key_from_metadata(body.metadata)
    """
    if not metadata:
        return None
    for field in _IDEMPOTENCY_BODY_FIELDS:
        value = metadata.get(field)
        # Return the un-stripped value: ``idempotency_for`` hashes the
        # RAW request bytes for replay-conflict detection, so any
        # normalisation here would let two byte-identical retries —
        # one with surrounding whitespace, one without — hit the same
        # cache bucket but mismatch on body hash, triggering a false
        # 422. The blank guard still rejects pure-whitespace values.
        if isinstance(value, str) and value.strip():
            return value
    return None


class IdempotencyGuard:
    """One per request-with-header. Replay or record, never both."""

    def __init__(
        self,
        tenant_id: str,
        key: str,
        request_hash: str,
        cached: dict | None,
    ) -> None:
        self.tenant_id = tenant_id
        self.key = key
        self.request_hash = request_hash
        self._cached = cached

    @property
    def cached_replay(self) -> tuple[Any, int] | None:
        """``(response_body, status_code)`` if the same key + body was
        served before and is still within TTL; else ``None``."""
        if self._cached is None:
            return None
        return self._cached["response_body"], self._cached["status_code"]

    async def record(self, response_body: Any, status_code: int = 200) -> None:
        """Cache the response body under this key. Failures are logged,
        not raised — the client already has the live response; losing the
        cache only costs a chance at future dedup."""
        expires_at = datetime.now(UTC) + timedelta(seconds=settings.idempotency_ttl_seconds)
        try:
            await get_storage_client().upsert_idempotency(
                tenant_id=self.tenant_id,
                idempotency_key=self.key,
                request_hash=self.request_hash,
                response_body=response_body,
                status_code=status_code,
                expires_at=expires_at.isoformat(),
            )
        except Exception:
            logger.warning("Idempotency record failed (non-critical)", exc_info=True)


async def idempotency_for(
    request: Request,
    tenant_id: str,
    idempotency_key: str | None,
    *,
    source: Literal["header", "body"] = "header",
) -> IdempotencyGuard | None:
    """Look up — and atomically claim — the inbox for (tenant, key).

    Returns None if the key was absent. On a cache hit with a
    *different* body hash, raises 422 — reusing the same key with
    different content is explicitly a client error per the IETF
    idempotency-key draft.

    Call this inside a route handler *after* auth enforcement, so the
    tenant_id passed here is already vetted.

    ``source`` selects a transport-specific prefix (``"header"`` →
    ``header:``, ``"body"`` → ``body:``) prepended before the lookup
    so the same string value sent via different transports never
    shares a cache bucket. Validation runs against the RAW key the
    caller supplied; the prefix is added only for storage.

    Body hashing note: uses raw request bytes. Clients retrying a write
    MUST send byte-identical bodies (same JSON serializer, same key
    ordering). Non-deterministic serialization will falsely flag a
    retry as a conflict. Matches Stripe and the IETF draft.

    Concurrency: closes the race where two requests with the same key
    arrive within milliseconds. The flow is lookup → claim → poll:

    1. ``get_idempotency`` reads any existing row.
    2. If a completed row matches the request hash, replay it.
    3. If no row, ``claim_idempotency`` atomically inserts a pending
       sentinel via INSERT ... ON CONFLICT DO NOTHING. The winning
       caller proceeds with the handler; ``record()`` flips
       ``is_pending`` to False.
    4. If the claim collided (a concurrent caller raced ahead between
       step 1 and step 3, or the row was pending in step 1), poll the
       existing row until ``is_pending`` is False, then replay.

    Storage outages degrade to "no cache" — a lost dedup opportunity
    beats a blocked write.
    """
    if not idempotency_key:
        return None
    if not idempotency_key.strip():
        raise HTTPException(status_code=400, detail="Idempotency-Key must not be blank")
    if len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Idempotency-Key must be at most {MAX_IDEMPOTENCY_KEY_LEN} characters",
        )

    namespaced_key = f"{source}:{idempotency_key}"

    body_bytes = await request.body()
    request_hash = hashlib.sha256(body_bytes).hexdigest()

    sc = get_storage_client()

    cached: dict | None
    try:
        cached = await sc.get_idempotency(tenant_id, namespaced_key)
    except Exception:
        logger.warning("Idempotency lookup failed (degrading to no-cache)", exc_info=True)
        return IdempotencyGuard(tenant_id, namespaced_key, request_hash, cached=None)

    if cached and not cached.get("is_pending", False):
        if cached["request_hash"] != request_hash:
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key reused with a different request body",
            )
        return IdempotencyGuard(tenant_id, namespaced_key, request_hash, cached=cached)

    if cached and cached.get("is_pending", False):
        # Hash-check before polling: a different body for the same key is
        # always a 422 regardless of the in-flight handler's outcome,
        # and the pending row already carries the original hash. Without
        # this short-circuit the loser burns the full poll budget before
        # getting the same 422.
        if cached["request_hash"] != request_hash:
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key reused with a different request body",
            )
        completed = await _poll_until_complete(sc, tenant_id, namespaced_key)
        if completed is None:
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key still in progress; retry shortly",
            )
        if completed == _POLL_STORAGE_ERROR:
            raise HTTPException(
                status_code=503,
                detail="Upstream storage unavailable; retry later",
            )
        if completed == _POLL_VANISHED:
            raise HTTPException(
                status_code=409,
                detail="Previous request did not complete; safe to retry",
            )
        assert isinstance(completed, dict)  # narrow for mypy after sentinel checks
        if completed["request_hash"] != request_hash:
            raise HTTPException(
                status_code=422,
                detail="Idempotency-Key reused with a different request body",
            )
        return IdempotencyGuard(tenant_id, namespaced_key, request_hash, cached=completed)

    # Claim with a short pending-TTL. ``record()`` later extends to the
    # full TTL on success. A crashed handler leaves the row pending only
    # until ``idempotency_pending_ttl_seconds`` elapses; after that the
    # expired-row reclaim path in ``idempotency_claim`` lets a fresh
    # request take over the key automatically.
    pending_expires_at = datetime.now(UTC) + timedelta(seconds=settings.idempotency_pending_ttl_seconds)
    try:
        claimed, existing = await sc.claim_idempotency(
            tenant_id=tenant_id,
            idempotency_key=namespaced_key,
            request_hash=request_hash,
            expires_at=pending_expires_at.isoformat(),
        )
    except Exception:
        logger.warning("Idempotency claim failed (degrading to no-cache)", exc_info=True)
        return IdempotencyGuard(tenant_id, namespaced_key, request_hash, cached=None)

    if claimed:
        return IdempotencyGuard(tenant_id, namespaced_key, request_hash, cached=None)

    if existing and existing["request_hash"] != request_hash:
        raise HTTPException(
            status_code=422,
            detail="Idempotency-Key reused with a different request body",
        )
    # Fast-path: the concurrent holder finished between our failed
    # INSERT and our follow-up SELECT — the existing row is already
    # complete and matches our hash. Skip the poll's GET roundtrip.
    if existing and not existing.get("is_pending", True):
        return IdempotencyGuard(tenant_id, namespaced_key, request_hash, cached=existing)
    completed = await _poll_until_complete(sc, tenant_id, namespaced_key)
    if completed is None:
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key still in progress; retry shortly",
        )
    if completed == _POLL_STORAGE_ERROR:
        raise HTTPException(
            status_code=503,
            detail="Upstream storage unavailable; retry later",
        )
    if completed == _POLL_VANISHED:
        raise HTTPException(
            status_code=409,
            detail="Previous request did not complete; safe to retry",
        )
    assert isinstance(completed, dict)  # narrow for mypy after sentinel checks
    if completed["request_hash"] != request_hash:
        raise HTTPException(
            status_code=422,
            detail="Idempotency-Key reused with a different request body",
        )
    return IdempotencyGuard(tenant_id, namespaced_key, request_hash, cached=completed)


async def _poll_until_complete(
    sc: Any,
    tenant_id: str,
    namespaced_key: str,
) -> dict | str | None:
    """Poll ``get_idempotency`` until the row's ``is_pending`` is False
    or the budget elapses.

    Returns:
    - the completed row dict on success
    - ``_POLL_VANISHED``: the row no longer exists (cleanup-job
      prune or expired pending TTL) — caller should tell the client
      to retry as a fresh request
    - ``_POLL_STORAGE_ERROR``: storage raised an exception (down,
      timeout, network) — caller should respond 503
    - ``None`` when the poll budget is exhausted while the row is
      still pending — caller should tell the client to wait and retry

    Checks first, sleeps after — a winner that finishes between the
    initial cache hit and entry to this loop should be visible
    immediately rather than after a guaranteed ``_CLAIM_POLL_INTERVAL``
    delay.
    """
    for attempt in range(_CLAIM_POLL_MAX_ATTEMPTS):
        try:
            row = await sc.get_idempotency(tenant_id, namespaced_key)
        except Exception:
            logger.warning("Idempotency poll failed", exc_info=True)
            return _POLL_STORAGE_ERROR
        if row is None:
            return _POLL_VANISHED
        if not row.get("is_pending", False):
            return row
        # Skip the sleep on the final iteration — the next thing we'd
        # do is exit the loop, so the wait would just add latency.
        if attempt < _CLAIM_POLL_MAX_ATTEMPTS - 1:
            await asyncio.sleep(_CLAIM_POLL_INTERVAL_SECONDS)
    return None
