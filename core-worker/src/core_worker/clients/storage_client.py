"""Async HTTP client for core-storage-api.

The worker hits a small set of endpoints:
* ``PATCH /api/v1/storage/memories/{memory_id}`` — write the embedding
  (CAURA-594) or enrichment fields (CAURA-595) back once the provider
  call succeeds.
* ``GET /api/v1/storage/memories/embedding-by-content-hash`` — best-
  effort cache lookup so an identical-content row that already has an
  embedding short-circuits the embed provider call.

Module-level singleton client (``get_storage_client()``) so connection
pooling persists across pull-loop iterations. Closed via
:func:`close_storage_client` from the FastAPI lifespan.

Every outbound request carries the storage service shared secret. On Cloud Run
it is also signed with an ID token whose audience is the storage service URL.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx

from common.constants import LIFECYCLE_STALE_ARCHIVE_WEIGHT
from common.http_retry import with_connect_phase_retry
from common.storage_auth import is_storage_shared_secret_rejection
from core_worker.clients.identity_token import evict as _evict_id_token
from core_worker.clients.identity_token import fetch_auth_header
from core_worker.config import settings

logger = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None
# Audience for the writer ID token — set alongside the singleton in
# ``get_storage_client()``. Mirrors core-api's ``self._base_url``: the
# rstrip'd ``core_storage_api_url`` from settings, used both as the
# httpx ``base_url`` and as the Cloud Run audience claim.
_audience: str | None = None
_storage_shared_secret: str | None = None
_PREFIX = "/api/v1/storage"


def get_storage_client() -> httpx.AsyncClient:
    """Return the singleton :class:`httpx.AsyncClient` used by the consumer.

    Configuration comes from the module singleton; the HTTP client itself is
    built on first call and reused until :func:`close_storage_client`.
    """
    global _client, _audience, _storage_shared_secret
    if _client is None:
        # ``rstrip("/")`` so a trailing slash in the env var doesn't
        # produce a different audience string than Cloud Run validates.
        _audience = settings.core_storage_api_url.rstrip("/")
        _storage_shared_secret = settings.core_storage_shared_secret.get_secret_value()
        _client = httpx.AsyncClient(
            base_url=_audience,
            timeout=httpx.Timeout(settings.storage_http_timeout_s),
        )
    return _client


async def close_storage_client() -> None:
    """Close the singleton (idempotent). Called from app lifespan shutdown."""
    global _client, _audience, _storage_shared_secret
    if _client is not None:
        await _client.aclose()
    _client = None
    _audience = None
    _storage_shared_secret = None


async def _signed_call(
    fn: Callable[..., Awaitable[httpx.Response]],
    *args: Any,
    **kwargs: Any,
) -> httpx.Response:
    """Issue ``fn(*args, **kwargs)`` with storage and Cloud Run credentials.

    ``fn`` is a bound method on the caller's ``httpx.AsyncClient``
    (``client.get``, ``client.patch``, etc.) — single integration point
    so a future endpoint helper can't add a request without auth or
    skip the eviction-on-401 path.

    On an identity-layer 401: evict the cached token (so a mid-TTL credential
    rotation or SA/binding fix self-heals instead of waiting out the 50 min
    cache), fetch a fresh token, and retry the request once. Storage's distinct
    shared-secret 401 skips that ineffective refresh. A second identity 401
    propagates to the caller's normal error-handling path.

    On the OSS / local-dev path the Cloud Run Authorization header may be
    absent, but ``X-Storage-Secret`` is still required. 403 is intentionally
    NOT a trigger for eviction: 403 means
    the token was accepted but IAM rejected the caller, which the
    cache cannot fix.

    Each attempt also retries connection-phase failures in-process via
    ``with_connect_phase_retry`` (policy + safety argument in
    ``common/http_retry.py``). Deliberately NOT the full idempotent
    retry set: the worker's designed retry path for sent-but-failed
    requests is raise → nack → Pub/Sub redelivery, and an in-process
    ReadTimeout/5xx retry would double-layer that. Connection-phase
    failures are the exception because each one otherwise burns a
    Pub/Sub delivery attempt against the DLQ budget for a request
    that never even reached storage (the 2026-06-11 incident shape).
    """
    global _storage_shared_secret
    headers = dict(kwargs.pop("headers", None) or {})
    if _storage_shared_secret is None:
        _storage_shared_secret = settings.core_storage_shared_secret.get_secret_value()
    if _storage_shared_secret:
        headers["X-Storage-Secret"] = _storage_shared_secret
    if _audience is not None:
        headers.update(await fetch_auth_header(_audience))
    method = getattr(fn, "__name__", "?").upper()
    path = args[0] if args else ""
    label = f"{method} {path}".rstrip()
    resp = await with_connect_phase_retry(lambda: fn(*args, headers=headers, **kwargs), label=label)
    if resp.status_code == 401 and _audience is not None and not is_storage_shared_secret_rejection(resp):
        _evict_id_token(_audience)
        # Rebind to a NEW dict rather than ``.update()`` in place — the
        # first call still holds a reference to the original headers
        # mapping (httpx is already done with it, but a debug logger
        # or test inspector that snapshots the call args would
        # otherwise see the rotated token retro-fitted into the first
        # call's record).
        fresh_headers = {**headers, **await fetch_auth_header(_audience)}
        resp = await with_connect_phase_retry(lambda: fn(*args, headers=fresh_headers, **kwargs), label=label)
    return resp


async def find_embedding_by_content_hash(
    client: httpx.AsyncClient,
    *,
    tenant_id: str,
    content_hash: str,
) -> list[float] | None:
    """Look up a previously computed embedding for the same content.

    Returns ``None`` when there's no cached row, the row exists but its
    embedding is NULL (still pending), or the storage call fails for
    any reason. Callers fall through to the embedding provider.

    Storage contract (``GET /memories/embedding-by-content-hash``)
    returns the bare ``list[float] | None`` — JSON ``null`` on miss,
    not ``{"embedding": null}``. Treating the body as a dict and
    calling ``.get("embedding")`` on a JSON-null parse crashes with
    ``AttributeError: 'NoneType' object has no attribute 'get'``,
    which used to be masked by 403s before the CAURA-595 ID-token
    PR landed and the cache lookup actually started succeeding.
    """
    try:
        resp = await _signed_call(
            client.get,
            f"{_PREFIX}/memories/embedding-by-content-hash",
            params={"tenant_id": tenant_id, "content_hash": content_hash},
        )
        if resp.status_code != 200:
            return None
        # ``resp.json()`` raises on malformed bodies (a partial response
        # from a stray proxy, an HTML error page sneaking through with
        # a 200, etc.). The docstring promises "fails for any reason →
        # None" so the worker falls through to a fresh provider call
        # instead of nacking and forcing Pub/Sub redelivery — staying
        # consistent with the ``HTTPError`` handling above.
        return resp.json()
    except (httpx.HTTPError, ValueError):
        logger.exception("storage embedding-cache lookup failed; treating as miss")
        return None


async def update_memory_embedding(
    client: httpx.AsyncClient,
    *,
    memory_id: UUID,
    tenant_id: str,
    embedding: list[float],
    embedded_content_hash: str | None = None,
) -> None:
    """PATCH the embedding onto the memory row.

    Raises on transient (5xx / 429) responses so the consumer nacks
    → Pub/Sub redelivers. Permanent 4xx errors (404, 422) are logged
    and ack-dropped — retrying them just burns the
    max-delivery-attempts budget against an event that will never
    succeed (row deleted between event publish and processing, or a
    schema mismatch the storage layer rejects either way).

    ``tenant_id`` is included in the body so the storage layer can
    guard against cross-tenant updates.

    The hot-path writer set ``metadata.embedding_pending=true`` when
    deferring the embed; clear it via ``metadata_patch`` on the same
    PATCH so a read-after-success returns clean state instead of
    confusingly claiming the row is still pending.

    ``embedded_content_hash`` records WHICH text this vector came from
    (migration 037). This is the deferred/SaaS write path, so without it
    every row embedded here would sit at ``unknown_provenance`` forever
    and the staleness detector would be blind to exactly the deployment
    mode that does the embedding.

    Sent as its own key rather than as ``content_hash``: storage stamps
    provenance from ``content_hash`` only when it is patching ``content``
    too, and ``content_hash`` is a real column — sending it here would
    WRITE it, overwriting the row's hash with a value from an event that
    may already be stale. The explicit key sets provenance alone and
    touches nothing else.

    Stays on ``PATCH /memories/{id}`` rather than the dedicated
    ``/embedding`` route: that route replaces ``metadata`` wholesale,
    which would clobber every key the ``metadata_patch`` merge above
    exists to preserve.
    """
    body: dict = {
        "tenant_id": tenant_id,
        "embedding": embedding,
        # B7 x C25 — clear BOTH homes: the C25 read view gives ``_system``
        # precedence, so legacy-only clearing would leave
        # ``system_metadata.embedding_pending`` True forever.
        "metadata_patch": {"embedding_pending": False, "_system": {"embedding_pending": False}},
    }
    if embedded_content_hash is not None:
        body["embedded_content_hash"] = embedded_content_hash
    resp = await _signed_call(
        client.patch,
        f"{_PREFIX}/memories/{memory_id}",
        json=body,
    )
    if resp.status_code == 404:
        # Row was deleted after the event was published — common-enough
        # legitimate race that it shouldn't churn the DLQ.
        logger.warning(
            "memory %s not found in storage; ack-dropping embed-request "
            "(row deleted between publish and processing)",
            memory_id,
        )
        return
    if resp.status_code == 422:
        # Storage rejected the payload shape. Won't pass on a redelivery
        # either — log loudly so the schema mismatch is visible and ack.
        logger.warning(
            "storage rejected embedding PATCH for memory %s with 422; ack-dropping",
            memory_id,
        )
        return
    # 5xx / 429 / network → raise → consumer nacks → Pub/Sub redelivers.
    resp.raise_for_status()


@dataclass(frozen=True)
class NullEmbeddingRow:
    """Identifiers for one memory row that needs re-embedding.

    Carries only the fields needed to address a follow-up
    ``GET /memories/{id}`` fetch. Raw ``content`` / ``content_hash``
    are NOT included — the listing endpoint deliberately returns
    ids-only (defence-in-depth on the unauthenticated storage API),
    and the worker fetches content per row before publishing.
    """

    memory_id: UUID
    tenant_id: str


async def iter_memories_with_null_embedding(
    client: httpx.AsyncClient,
    *,
    tenant_id: str,
    batch_size: int = 500,
) -> AsyncIterator[list[NullEmbeddingRow]]:
    """Paginate memories whose ``embedding IS NULL`` for one tenant.

    Yields lists of up to *batch_size* rows so the caller can publish
    events in batches and apply backpressure. Stops when the storage
    API reports an empty page.

    Idempotent under restart: each call re-queries from the start, but
    rows the consumer has since embedded are no longer NULL and are
    skipped naturally by the storage-side filter.

    Transient HTTP errors propagate — the operator-driven CLI prefers
    a loud failure over a silent partial-scan.

    ``tenant_id`` is **required**. The storage-API endpoint refuses
    un-scoped requests because the OSS API has no auth middleware.
    For whole-deployment scans, iterate the tenant list and call once
    per tenant.
    """
    after: str | None = None
    while True:
        params: dict[str, Any] = {"limit": batch_size, "tenant_id": tenant_id}
        if after is not None:
            params["after"] = after
        resp = await _signed_call(
            client.get,
            f"{_PREFIX}/memories/null-embedding-ids",
            params=params,
        )
        resp.raise_for_status()
        page = resp.json()
        rows_payload = page.get("rows") or []
        if not rows_payload:
            # Endpoint contract: when ``rows`` is non-empty,
            # ``next_after`` is always a UUID string (the last row's
            # id). When ``rows`` is empty, we're done — no need for a
            # secondary ``next_after is None`` check, which used to
            # follow the yield below but was unreachable under the
            # contract.
            return
        yield [
            NullEmbeddingRow(
                memory_id=UUID(r["id"]),
                tenant_id=r["tenant_id"],
            )
            for r in rows_payload
        ]
        after = page["next_after"]


async def get_memory(
    client: httpx.AsyncClient,
    *,
    memory_id: UUID,
    tenant_id: str,
) -> dict:
    """Fetch a single memory by id, scoped to ``tenant_id``.

    Used by the embedding backfill worker to retrieve ``content`` +
    ``content_hash`` after the listing endpoint hands it an id. The
    listing path deliberately returns ids-only so this round-trip
    here is the only place raw memory content is exposed over the
    HTTP boundary, on a per-row basis (rate-limitable, audit-loggable
    if the storage API later adds auth middleware).

    Raises ``HTTPStatusError`` on 404 or any non-2xx — the caller
    decides whether to skip-and-continue (the row was deleted between
    listing and fetch) or abort.
    """
    resp = await _signed_call(
        client.get,
        f"{_PREFIX}/memories/{memory_id}",
        params={"tenant_id": tenant_id},
    )
    resp.raise_for_status()
    return resp.json()


async def archive_expired(
    client: httpx.AsyncClient,
    *,
    tenant_id: str,
    fleet_id: str | None,
) -> int:
    """Run the storage-side ``memory_archive_expired`` SQL primitive.

    Returns the row count flipped from ``active`` to ``outdated``;
    surfaced as ``stats.archived`` on the lifecycle_audit row.
    """
    body: dict[str, Any] = {"tenant_id": tenant_id}
    if fleet_id is not None:
        body["fleet_id"] = fleet_id
    resp = await _signed_call(
        client.post,
        f"{_PREFIX}/memories/archive-expired",
        json=body,
    )
    resp.raise_for_status()
    return resp.json().get("count", 0)


async def archive_stale(
    client: httpx.AsyncClient,
    *,
    tenant_id: str,
    fleet_id: str | None,
) -> int:
    """Run the storage-side ``memory_archive_stale`` SQL primitive.

    ``max_weight`` is sent explicitly so the cron-driven worker path
    matches the synchronous code path's threshold rather than relying
    on the storage server's default to stay aligned. The shared
    constant lives in :mod:`common.constants` so core-api's adapter
    and this worker can't silently diverge.
    """
    body: dict[str, Any] = {
        "tenant_id": tenant_id,
        "max_weight": LIFECYCLE_STALE_ARCHIVE_WEIGHT,
    }
    if fleet_id is not None:
        body["fleet_id"] = fleet_id
    resp = await _signed_call(
        client.post,
        f"{_PREFIX}/memories/archive-stale",
        json=body,
    )
    resp.raise_for_status()
    return resp.json().get("count", 0)


async def purge_soft_deleted(
    client: httpx.AsyncClient,
    *,
    tenant_id: str,
    fleet_id: str | None,
    retention_days: int,
) -> int:
    """Run the storage-side ``memory_purge_soft_deleted`` SQL primitive.

    ``retention_days`` is REQUIRED — it rides along in the Pub/Sub
    payload, snapshot at fanout time from the org's
    ``lifecycle.memory_retention_days`` setting. Defaulting it on this
    side would mask a publisher bug that forgot to bake the value in.
    """
    body: dict[str, Any] = {"tenant_id": tenant_id, "retention_days": retention_days}
    if fleet_id is not None:
        body["fleet_id"] = fleet_id
    resp = await _signed_call(
        client.post,
        f"{_PREFIX}/memories/purge-soft-deleted",
        json=body,
    )
    resp.raise_for_status()
    return resp.json().get("deleted", 0)


async def upsert_tenant_suppression(
    client: httpx.AsyncClient,
    *,
    tenant_id: str,
    action: str,
    updated_by: str | None,
) -> None:
    """POST one tenant_suppression upsert (CAURA-694).

    ``action`` is ``"suppress"`` | ``"restore"``; the storage service
    validates the value and ours stays a pass-through so the wire shape
    has one source of truth. ``updated_by`` propagates the
    correlation id from the bus event for audit-trail use.
    """
    body: dict[str, Any] = {"tenant_id": tenant_id, "action": action}
    if updated_by is not None:
        body["updated_by"] = updated_by
    resp = await _signed_call(
        client.post,
        f"{_PREFIX}/tenant-suppression",
        json=body,
    )
    resp.raise_for_status()


async def update_lifecycle_audit_row(
    client: httpx.AsyncClient,
    audit_id: int,
    *,
    status: str,
    stats: dict | None = None,
    error_message: str | None = None,
) -> None:
    """PATCH the lifecycle_audit row created by core-api fanout."""
    body: dict[str, Any] = {"status": status}
    if stats is not None:
        body["stats"] = stats
    if error_message is not None:
        body["error_message"] = error_message
    resp = await _signed_call(
        client.patch,
        f"{_PREFIX}/lifecycle-audit/{audit_id}",
        json=body,
    )
    if resp.status_code == 404:
        # Audit row pruned or never created. The shared handler decides
        # whether to log + continue (in_progress) or surface (final).
        logger.warning(
            "lifecycle_audit %s not found on PATCH (status=%s)",
            audit_id,
            status,
        )
        return
    resp.raise_for_status()


async def update_memory_enrichment(
    client: httpx.AsyncClient,
    *,
    memory_id: UUID,
    tenant_id: str,
    fields: dict,
) -> None:
    """PATCH enrichment-output fields onto the memory row.

    Mirrors :func:`update_memory_embedding` for the
    ``Topics.Memory.ENRICH_REQUESTED`` worker path. ``fields`` is the
    pre-validated dict of column→value pairs the consumer wants to write
    (memory_type, weight, title, summary, tags, status, ts_valid_*,
    contains_pii, pii_types, retrieval_hint). The storage-side
    ``memory_update`` filters by ``_MEMORY_UPDATABLE_FIELDS`` so unknown keys
    are silently dropped — caller still validates upstream against
    ``EnrichmentResult`` so a typo surfaces as a Pydantic error, not a
    silent no-op.

    Idempotency: at-least-once delivery means a redelivered enrich event
    re-PATCHes with the (deterministic up to LLM noise) same fields.
    Safe — the storage update is a write-after-write.

    Same 404/422 short-circuits as :func:`update_memory_embedding`.
    """
    if not fields:
        # Defensive — heuristic fallback may produce an all-defaults
        # result; an empty PATCH is wasted work and noisy in storage logs.
        logger.debug("skipping empty enrichment PATCH for memory %s", memory_id)
        return
    resp = await _signed_call(
        client.patch,
        f"{_PREFIX}/memories/{memory_id}",
        json={"tenant_id": tenant_id, **fields},
    )
    if resp.status_code == 404:
        logger.warning(
            "memory %s not found in storage; ack-dropping enrich-request "
            "(row deleted between publish and processing)",
            memory_id,
        )
        return
    if resp.status_code == 422:
        logger.warning(
            "storage rejected enrichment PATCH for memory %s with 422; ack-dropping",
            memory_id,
        )
        return
    resp.raise_for_status()
