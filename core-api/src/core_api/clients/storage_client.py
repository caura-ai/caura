"""HTTP client for the core-storage-api service."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal, NotRequired, TypedDict

import httpx

from common.events.lifecycle_purge_request import MEMORY_RETENTION_MAX_DAYS
from common.http_retry import CONNECT_PHASE_MAX_ATTEMPTS, with_connect_phase_retry, with_retry
from core_api.clients.identity_token import evict as _evict_id_token
from core_api.clients.identity_token import fetch_auth_header
from core_api.config import settings

logger = logging.getLogger(__name__)


# Retry policy (F5 + the 2026-06-11 connect-timeout incident) lives in
# ``common/http_retry.py``, shared with core-worker's storage client:
# GET/PATCH/DELETE retry the full transient set + retryable 5xx;
# POSTs retry connection-phase failures only (request provably never
# sent, so a retry cannot double-insert).


# Idempotent reads (GET) ride out a storage cold start / instance recycle on
# the connection-phase retry budget (CONNECT_PHASE_MAX_ATTEMPTS) — a connect
# failure is provably never sent, so the extra attempts add no load — while
# ReadTimeout/5xx stay at the default 3. Without this, a multi-second storage
# blip exhausted the 3 GET attempts and surfaced on the Pub/Sub enrichment
# path as a handler failure → nack → redelivery (the "pubsub handler raised"
# tail; the POST connect-phase path was already on this budget).
async def _read_retry(do_request: Callable[[], Awaitable[httpx.Response]], *, label: str) -> httpx.Response:
    return await with_retry(do_request, label=label, connect_phase_max_attempts=CONNECT_PHASE_MAX_ATTEMPTS)


class KeystoneUpsertPayload(TypedDict):
    """Shape of the body POSTed to ``/keystones`` on core-storage.

    Required fields match the storage-side validator's required set
    (storage 422s on any missing one). Optional fields use
    ``NotRequired`` so callers can omit them — both surfaces strip
    ``None`` before posting because storage distinguishes a present
    ``"fleet_id": null`` from an absent key.
    """

    tenant_id: str
    doc_id: str
    title: str
    content: str
    scope: Literal["tenant", "fleet", "agent"]
    weight: Literal["low", "med", "high"]
    fleet_id: NotRequired[str]
    agent_id: NotRequired[str]
    author_user_id: NotRequired[str]


_client: CoreStorageClient | None = None


def get_storage_client() -> CoreStorageClient:
    """Return the singleton storage client."""
    global _client
    if _client is None:
        _client = CoreStorageClient()
    return _client


class CoreStorageClient:
    """Async HTTP client for core-storage-api CRUD operations."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        read_url: str | None = None,
        http: httpx.AsyncClient | None = None,
        read_http: httpx.AsyncClient | None = None,
    ) -> None:
        """Construct a client. All arguments are optional and default to
        what ``settings`` and ``_make_pool()`` provide; tests use the
        overrides to inject an ASGI-bridged httpx pool instead of the
        real HTTP one. A unified constructor (rather than a separate
        ``__new__``-based test builder) means any new attribute added
        here is automatically populated in both prod and test paths.
        """
        self._base_url = (base_url or settings.core_storage_api_url).rstrip("/")
        self._prefix = f"{self._base_url}/api/v1/storage"
        self._http = http or self._make_pool()

        # Optional separate endpoint for reads — point reader-role
        # core-storage instances at a Postgres read replica so the writer
        # fleet stays small and connection-pool contention on the primary
        # stays low. Empty = no split; reads and writes share ``_http``
        # and behaviour is unchanged.
        configured_read_url = read_url if read_url is not None else settings.core_storage_read_url
        self._read_base_url = (configured_read_url or self._base_url).rstrip("/")
        self._read_prefix = f"{self._read_base_url}/api/v1/storage"
        if read_http is not None:
            self._read_http: httpx.AsyncClient = read_http
        elif configured_read_url and self._read_base_url != self._base_url:
            self._read_http = self._make_pool()
        else:
            # Share one pool when the split isn't configured — or when an
            # operator accidentally points both URLs at the same upstream.
            # Otherwise we'd double the connection budget (400 vs 200 max)
            # against a single service for no benefit.
            self._read_http = self._http

        # Pool self-healing (incident 2026-06-16): the singleton pool could
        # leak connection slots when in-flight calls were cancelled and never
        # recovered without a process restart. ``_pool_generation`` lets
        # concurrent ``PoolTimeout``s collapse into exactly one rebuild.
        self._pool_lock = asyncio.Lock()
        self._pool_generation = 0
        # Anchor fire-and-forget pool-close tasks so they aren't GC'd while
        # pending (Python 3.12+ warns otherwise); discard on completion.
        self._background_tasks: set[asyncio.Task] = set()

    @staticmethod
    def _make_pool() -> httpx.AsyncClient:
        """Construct an httpx pool with the tuned timeouts + limits.

        Per-phase timeouts: connect/pool are short (5s) to fail fast on
        network issues; read/write are generous (120s) for heavy bulk
        writes where storage-side commits can be slow under load.

        The 120s read budget is deliberately ABOVE the bulk route's own
        90s ``bulk_request_timeout_seconds`` (CAURA-602) so
        ``asyncio.wait_for`` almost always wins the race against an
        httpx-level timeout: the route layer cancels cleanly, returns
        a 504 with the ``X-Bulk-Attempt-Id`` retry hint, and the
        per-row attempt-id constraint recovers any committed rows.
        Equal values would 50/50 race — if httpx fired first the
        request propagated as 500 with no retry contract, which
        was the silent-create regression mode. The route still
        catches ``httpx.TimeoutException`` as a defence-in-depth
        fallback for the remaining 1%.
        """
        return httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=120.0, write=120.0, pool=5.0),
            # CAURA-682: pre-fix values 100/50 caused TCP ConnectTimeout
            # retries (3x 5s ~= 13s tail per affected request) during
            # noisy-neighbor write storms — concurrent core-api → storage
            # demand exceeded keepalive ceiling, forcing new TCP
            # handshakes that piled up at the Cloud Run frontend.
            # Sized for 20 concurrent storm writes x ~5 storage calls
            # each = 100 in-flight at peak, plus tenant-B probe headroom
            # and a 33% burst margin.
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=150),
            follow_redirects=True,
        )

    @classmethod
    def for_testing(cls, base_url: str, http: httpx.AsyncClient) -> CoreStorageClient:
        """Test-only constructor: bind an already-built httpx client
        (usually an ASGI bridge) as both writer and reader pools. Just
        forwards to ``__init__`` so both construction paths share code.

        ``read_url=""`` is passed explicitly so tests never pick up a
        stale ``CORE_STORAGE_READ_URL`` from the surrounding environment —
        otherwise ``_read_prefix`` would point at a staging / prod reader
        while the httpx client is the in-process ASGI bridge.
        """
        return cls(base_url=base_url, read_url="", http=http, read_http=http)

    async def close(self) -> None:
        # try/finally: if the writer pool raises on close, still release
        # the reader pool (otherwise it leaks across test sessions and on
        # crashed-then-restarted app instances).
        try:
            await self._http.aclose()
        finally:
            if self._read_http is not self._http:
                await self._read_http.aclose()

    # -- pool resilience (incident 2026-06-16) ---------------------------

    async def _cancel_safe(self, coro: Awaitable[httpx.Response]) -> httpx.Response:
        """Run an in-flight storage request so cancellation cannot strand its
        pooled connection.

        When the 35s enrichment ``wait_for`` or the 45s request-timeout
        cancels the awaiting task mid-request, httpcore (1.0.9, current)
        does not return the connection to the pool — leaked slots accumulate
        until the ``max_connections=200`` ceiling is hit and every acquire
        ``PoolTimeout``s (incident 2026-06-16). Shielding lets the request
        run to completion (returning its slot) while the caller still
        observes ``CancelledError`` immediately; the orphaned result is
        consumed so it is not logged as never-retrieved.
        """
        task = asyncio.create_task(coro)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:

            def _consume_result(t: asyncio.Task) -> None:
                if t.cancelled():
                    return
                exc = t.exception()
                if exc is not None:
                    logger.debug("storage_client: shielded request failed after caller cancellation: %r", exc)

            task.add_done_callback(_consume_result)
            raise

    @staticmethod
    async def _safe_aclose(client: httpx.AsyncClient) -> None:
        try:
            await client.aclose()
        except Exception as exc:  # pool teardown must never surface to callers
            logger.warning("storage_client: error closing recycled pool: %r", exc)

    async def _recycle_pools(self, *, observed_gen: int, label: str) -> None:
        """Rebuild the httpx pool(s) after exhaustion so a leaked-out singleton
        self-heals in seconds instead of requiring a process restart. The
        generation guard means concurrent ``PoolTimeout``s trigger exactly one
        rebuild; later arrivals see the bumped generation and return."""
        async with self._pool_lock:
            if self._pool_generation != observed_gen:
                return
            old_write, old_read = self._http, self._read_http
            shared = old_read is old_write
            self._http = self._make_pool()
            self._read_http = self._http if shared else self._make_pool()
            self._pool_generation += 1
            logger.error(
                "storage_client.%s: core-storage pool exhausted (PoolTimeout) — "
                "recycled connection pool (generation %d→%d); see incident 2026-06-16",
                label,
                observed_gen,
                self._pool_generation,
            )
        # Force-close the old pool(s) in the background. Safe to drop mid-flight:
        # a pool only reaches this path once it can no longer hand out
        # connections, so there is no healthy in-flight work left to disrupt.
        for old in {old_write, old_read}:
            t = asyncio.create_task(self._safe_aclose(old))
            self._background_tasks.add(t)
            t.add_done_callback(self._background_tasks.discard)

    async def _execute(
        self,
        do_request: Callable[[], Awaitable[httpx.Response]],
        *,
        retry: Callable[..., Awaitable[httpx.Response]],
        label: str,
    ) -> httpx.Response:
        """Run ``do_request`` through the ``retry`` policy, made cancellation-safe
        and pool-self-healing. ``do_request`` must read ``self._http`` /
        ``self._read_http`` lazily so a recycled pool is picked up on retry.
        If ``PoolTimeout`` survives the retry policy the pool is exhausted —
        recycle it once and retry on the fresh pool."""

        def _shielded() -> Awaitable[httpx.Response]:
            return self._cancel_safe(do_request())

        observed_gen = self._pool_generation
        try:
            return await retry(_shielded, label=label)
        except httpx.PoolTimeout:
            await self._recycle_pools(observed_gen=observed_gen, label=label)
            return await retry(_shielded, label=label)

    # -- internal helpers ------------------------------------------------

    async def _auth_headers(self, *, read: bool) -> dict[str, str]:
        """Identity-token Authorization header for Cloud Run
        ``--no-allow-unauthenticated`` targets (CAURA-591 Part B Y3).
        Empty when no credentials available (tests / local / legacy
        allUsers services). The dict is cached and shared per audience
        — httpx merges headers without mutation, so this is safe.

        Skip the metadata-server call entirely when the audience is
        plain HTTP — Cloud Run ``--no-allow-unauthenticated`` always
        uses TLS, so an ``http://`` audience is by definition local
        or in-cluster and never needs an ID token. Without this guard,
        the metadata-server fetch's 5 s timeout races the health
        probe's own 5 s budget; a lost race surfaces ``CancelledError``
        (not ``Exception``) which the inner catch misses, and the
        health endpoint flips to ``storage: unreachable`` for the
        duration of the failure-cache TTL."""
        audience = self._read_base_url if read else self._base_url
        if audience.startswith("http://"):
            return {}
        return await fetch_auth_header(audience)

    def _maybe_evict_on_auth_error(self, resp: httpx.Response, *, read: bool) -> None:
        """If the target rejected our token, drop it from the cache so
        the next request forces a fresh fetch. Self-heals after a
        mid-TTL credential rotation or SA/binding fix instead of
        making the operator wait out the 50 min cache."""
        if resp.status_code == 401:
            audience = self._read_base_url if read else self._base_url
            _evict_id_token(audience)

    async def _get(self, path: str, *, read: bool = True, **params: Any) -> dict | None:
        prefix = self._read_prefix if read else self._prefix
        headers = await self._auth_headers(read=read)

        def _do() -> Awaitable[httpx.Response]:
            # Read the pool lazily so a mid-call recycle is picked up on retry.
            http = self._read_http if read else self._http
            return http.get(f"{prefix}{path}", params=params, headers=headers)

        resp = await self._execute(_do, retry=_read_retry, label=f"GET {path}")
        if resp.status_code == 404:
            return None
        self._maybe_evict_on_auth_error(resp, read=read)
        resp.raise_for_status()
        return resp.json()

    async def _get_list(self, path: str, **params: Any) -> list[dict]:
        # All current _get_list callers are pure list/stats endpoints; none
        # sit on the write path, so no per-call opt-out is needed yet.
        headers = await self._auth_headers(read=True)

        def _do() -> Awaitable[httpx.Response]:
            return self._read_http.get(f"{self._read_prefix}{path}", params=params, headers=headers)

        resp = await self._execute(_do, retry=_read_retry, label=f"GET-list {path}")
        self._maybe_evict_on_auth_error(resp, read=True)
        resp.raise_for_status()
        return resp.json()

    async def _post(
        self, path: str, data: Any = None, *, read: bool = False, idempotent: bool = False
    ) -> dict | list:
        prefix = self._read_prefix if read else self._prefix
        headers = await self._auth_headers(read=read)

        def _do() -> Awaitable[httpx.Response]:
            http = self._read_http if read else self._http
            return http.post(
                f"{prefix}{path}",
                json=data if data is not None else {},
                headers=headers,
            )

        # Non-idempotent POSTs retry connection-phase failures ONLY (the request
        # was provably never sent). ``idempotent=True`` opts a POST into the full
        # transient set (ReadTimeout + 5xx too) — safe ONLY when the endpoint
        # dedups replays storage-side (e.g. /audit-logs/bulk on client_event_id).
        retry = with_retry if idempotent else with_connect_phase_retry
        resp = await self._execute(_do, retry=retry, label=f"POST {path}")
        self._maybe_evict_on_auth_error(resp, read=read)
        resp.raise_for_status()
        return resp.json()

    async def _patch(self, path: str, data: dict) -> dict | None:
        headers = await self._auth_headers(read=False)

        def _do() -> Awaitable[httpx.Response]:
            return self._http.patch(f"{self._prefix}{path}", json=data, headers=headers)

        resp = await self._execute(_do, retry=with_retry, label=f"PATCH {path}")
        if resp.status_code == 404:
            return None
        self._maybe_evict_on_auth_error(resp, read=False)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str, **params: Any) -> bool:
        headers = await self._auth_headers(read=False)

        def _do() -> Awaitable[httpx.Response]:
            return self._http.delete(f"{self._prefix}{path}", params=params, headers=headers)

        resp = await self._execute(_do, retry=with_retry, label=f"DELETE {path}")
        if resp.status_code == 404:
            return False
        self._maybe_evict_on_auth_error(resp, read=False)
        resp.raise_for_status()
        return True

    async def _post_optional(self, path: str, data: Any = None, *, read: bool = False) -> dict | None:
        prefix = self._read_prefix if read else self._prefix
        headers = await self._auth_headers(read=read)

        def _do() -> Awaitable[httpx.Response]:
            http = self._read_http if read else self._http
            return http.post(
                f"{prefix}{path}",
                json=data if data is not None else {},
                headers=headers,
            )

        resp = await self._execute(_do, retry=with_connect_phase_retry, label=f"POST {path}")
        if resp.status_code == 404:
            return None
        self._maybe_evict_on_auth_error(resp, read=read)
        resp.raise_for_status()
        return resp.json()

    # =====================================================================
    # Memories
    # =====================================================================

    async def create_memory(self, data: dict) -> dict:
        return await self._post("/memories", data)

    async def create_memories(self, data: list[dict]) -> list[dict]:
        """Bulk-insert memories with per-attempt idempotency (CAURA-602).

        Every input item must include ``client_request_id`` — the
        bulk endpoint derives it from ``X-Bulk-Attempt-Id`` upstream;
        in-process callers (auto-chunk) generate a UUID per item.

        Returns one entry per input item, in input order::

            {"client_request_id": str, "id": str | None, "was_inserted": bool}

        ``was_inserted=True`` means this attempt's row newly committed.
        ``False`` means the same ``(tenant_id, fleet_id, client_request_id)``
        was already in the table from a prior attempt — the canonical id
        is returned in ``id``. ``id is None`` is the rare third bucket
        (concurrent soft-delete or a torn write); the upstream caller
        treats it as a per-item error.
        """
        return await self._post("/memories/bulk", data)

    async def get_memory(self, memory_id: str) -> dict | None:
        return await self._get(f"/memories/{memory_id}")

    async def get_memory_for_tenant(self, tenant_id: str, memory_id: str) -> dict | None:
        return await self._get(f"/memories/{memory_id}", tenant_id=tenant_id)

    async def update_memory(self, memory_id: str, data: dict) -> dict | None:
        return await self._patch(f"/memories/{memory_id}", data)

    async def soft_delete_memory(self, memory_id: str) -> bool:
        return await self._delete(f"/memories/{memory_id}")

    async def update_memory_status(
        self,
        memory_id: str,
        status: str,
        supersedes_id: str | None = None,
        *,
        unset_supersedes: bool = False,
        expected_supersedes_id: str | None = None,
    ) -> dict | None:
        """Update status and optionally set or clear ``supersedes_id``.

        Set path (existing behaviour):
            ``supersedes_id=<uuid>`` sets the row's pointer, guarded by
            a CAS against NULL so the first detection wins.

        Clear path (A4 #10 — retraction):
            ``unset_supersedes=True`` plus ``expected_supersedes_id=<uuid>``
            clears the row's pointer, guarded by a CAS that requires the
            current value to either match the expected uuid or already be
            NULL. A current pointer to *a different* uuid yields a 409 so
            the caller knows another writer took the row.

        Raises
        ------
        ValueError
            If ``unset_supersedes=True`` without ``expected_supersedes_id`` —
            clearing without a CAS anchor would race concurrent setters.
            If both ``supersedes_id`` (set) and ``unset_supersedes=True``
            (clear) are passed in one call — contradictory intent.
        """
        if unset_supersedes:
            if supersedes_id is not None:
                raise ValueError(
                    "supersedes_id (set) and unset_supersedes=True (clear) "
                    "are mutually exclusive — choose one."
                )
            if expected_supersedes_id is None:
                raise ValueError(
                    "unset_supersedes=True requires expected_supersedes_id "
                    "for the CAS anchor; clearing without one would race "
                    "concurrent setters."
                )
        payload: dict[str, Any] = {"status": status}
        if supersedes_id is not None:
            payload["supersedes_id"] = supersedes_id
        if unset_supersedes:
            payload["unset_supersedes"] = True
            payload["expected_supersedes_id"] = expected_supersedes_id
        return await self._patch(f"/memories/{memory_id}/status", payload)

    async def find_by_content_hash(
        self,
        tenant_id: str,
        content_hash: str,
        fleet_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict | None:
        params: dict[str, Any] = {"tenant_id": tenant_id, "content_hash": content_hash}
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        if agent_id is not None:
            # Per-agent dedup scope (Stage 5 / friction §2.8): cross-agent
            # writes of identical content no longer collide. Omit to
            # preserve legacy fleet-wide dedup.
            params["agent_id"] = agent_id
        # Write-path exact-hash dedup gate — stale replica data here would
        # let a just-written duplicate slip through. Pair with
        # bulk_find_by_content_hashes and find_semantic_duplicate.
        return await self._get("/memories/by-content-hash", read=False, **params)

    async def find_embedding_by_content_hash(
        self,
        tenant_id: str,
        content_hash: str,
    ) -> list[float] | None:
        # Write-path embedding-cache lookup: skips re-embedding identical
        # content. Stale replica would cause a miss and trigger an
        # unnecessary (and expensive) re-embed.
        return await self._get(
            "/memories/embedding-by-content-hash",
            read=False,
            tenant_id=tenant_id,
            content_hash=content_hash,
        )

    async def find_duplicate_hash(
        self,
        tenant_id: str,
        content_hash: str,
        exclude_id: str | None = None,
    ) -> dict | None:
        params: dict[str, Any] = {"tenant_id": tenant_id, "content_hash": content_hash}
        if exclude_id is not None:
            params["exclude_id"] = exclude_id
        # Write-path: called during memory-update dedup. Reader would
        # miss a just-updated row and fail to detect the duplicate.
        return await self._get("/memories/duplicate-hash", read=False, **params)

    async def bulk_find_by_content_hashes(
        self,
        tenant_id: str,
        hashes: list[str],
        fleet_id: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, dict]:
        """Look up existing rows by content_hash for the dedup gate.

        Returns ``{content_hash: {"id": str, "client_request_id": str | None}}``.
        ``client_request_id`` lets the bulk path tell ``duplicate_attempt``
        (the caller's own retry) apart from ``duplicate_content``
        (different attempt, same content). ``agent_id`` scopes the lookup
        per-agent (Stage 5) so a batch from agent-A and a batch from
        agent-B in the same fleet don't collide on identical content.

        Explicit ``read=False``: this is the dedup lookup called inline
        during bulk writes. Routing to a read replica risks missing a
        just-written row and re-creating a duplicate. Matches
        postgres_service's get_session() choice for dedup (CAURA-591 A).
        Not relying on the default so a future flip of _post's default
        can't silently re-route it.
        """
        body: dict[str, Any] = {"tenant_id": tenant_id, "hashes": hashes}
        if fleet_id is not None:
            body["fleet_id"] = fleet_id
        if agent_id is not None:
            body["agent_id"] = agent_id
        result = await self._post(
            "/memories/bulk-by-content-hashes",
            body,
            read=False,
        )
        return result  # type: ignore[return-value]

    async def find_semantic_duplicate(self, data: dict) -> dict | None:
        # Explicit ``read=False``: runs inline during writes as a dedup
        # gate. Stale replica data would let a just-written near-dup slip
        # through. See bulk_find_by_content_hashes for the same reasoning.
        return await self._post_optional("/memories/semantic-duplicate", data, read=False)

    # ------------------------------------------------------------------
    # A1 #18 — Dedup review queue
    # ------------------------------------------------------------------

    async def enqueue_dedup_review(self, payload: dict) -> dict:
        """Append a row to the ambiguous-dedup review queue. Caller-side
        wraps this in ``track_task`` to keep the write path off the
        queue's failure modes."""
        return await self._post("/memories/dedup-reviews", payload, read=False)  # type: ignore[return-value]

    async def list_dedup_reviews(self, params: dict) -> list[dict]:
        """List reviews for a tenant, default ``status='pending'``."""
        return await self._get(  # type: ignore[return-value]
            "/memories/dedup-reviews",
            read=True,
            **{k: v for k, v in params.items() if v is not None},
        )

    async def decide_dedup_review(self, review_id, status: str, *, decided_by: str | None = None) -> dict:
        """Record a terminal decision (confirmed_duplicate /
        override_not_duplicate / dismissed) on a review row."""
        return await self._post(  # type: ignore[return-value]
            f"/memories/dedup-reviews/{review_id}/decision",
            {"status": status, "decided_by": decided_by},
            read=False,
        )

    async def find_entity_overlap_candidates(self, data: dict) -> list[dict]:
        # Called from contradiction_detector.py as a *post-commit* async
        # background task. Replica lag here only risks missing a
        # contradiction in the current batch — the write has already
        # committed, so there's no duplicate-row race. A missed detection
        # is picked up by a later crystallizer pass.
        return await self._post("/memories/entity-overlap-candidates", data, read=True)  # type: ignore[return-value]

    async def find_successors(self, data: dict) -> list[dict]:
        return await self._post("/memories/find-successors", data, read=True)  # type: ignore[return-value]

    async def find_similar_candidates(self, data: dict) -> list[dict]:
        # Same reasoning as find_entity_overlap_candidates: contradiction
        # detection runs post-commit and async. Stale replica data means
        # at most a missed contradiction, never a data-integrity issue.
        return await self._post("/memories/similar-candidates", data, read=True)  # type: ignore[return-value]

    async def find_rdf_conflicts(
        self,
        tenant_id: str,
        subject_entity_id: str,
        predicate: str,
        exclude_id: str | None = None,
        fleet_id: str | None = None,
        object_value: str | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "subject_entity_id": subject_entity_id,
            "predicate": predicate,
        }
        if exclude_id is not None:
            params["exclude_id"] = exclude_id
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        if object_value is not None:
            params["object_value"] = object_value
        return await self._get_list("/memories/rdf-conflicts", **params)

    async def scored_search(self, data: dict) -> list[dict]:
        return await self._post("/memories/scored-search", data, read=True)  # type: ignore[return-value]

    async def load_memories_by_ids(self, data: dict) -> list[dict]:
        """ENTITY_LOOKUP short-circuit endpoint (CAURA-687).

        Skips vector/FTS/freshness scoring — caller supplies memory IDs,
        server applies visibility/fleet/agent filters and returns raw
        memory rows. Pairs with PostgresService.memory_load_by_ids.
        """
        return await self._post("/memories/load-by-ids", data, read=True)  # type: ignore[return-value]

    async def archive_expired(self, tenant_id: str, fleet_id: str | None = None) -> int:
        data: dict[str, Any] = {"tenant_id": tenant_id}
        if fleet_id is not None:
            data["fleet_id"] = fleet_id
        result = await self._post("/memories/archive-expired", data)
        return result.get("count", 0)  # type: ignore[union-attr]

    async def archive_stale(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
        max_weight: float = 0.3,
    ) -> int:
        data: dict[str, Any] = {"tenant_id": tenant_id, "max_weight": max_weight}
        if fleet_id is not None:
            data["fleet_id"] = fleet_id
        result = await self._post("/memories/archive-stale", data)
        return result.get("count", 0)  # type: ignore[union-attr]

    async def purge_soft_deleted(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
        retention_days: int = MEMORY_RETENTION_MAX_DAYS,
    ) -> int:
        data: dict[str, Any] = {"tenant_id": tenant_id, "retention_days": retention_days}
        if fleet_id is not None:
            data["fleet_id"] = fleet_id
        result = await self._post("/memories/purge-soft-deleted", data)
        return result.get("deleted", 0)  # type: ignore[union-attr]

    async def purge_tenant_data(self, tenant_id: str) -> dict[str, int]:
        """Hard-delete ALL OSS data for ``tenant_id`` (CAURA-689 org delete).

        Returns the per-table deleted counts. Idempotent at the storage
        layer — a repeat call for an already-purged tenant returns zeros.
        """
        result = await self._post("/purge/tenant-data", {"tenant_id": tenant_id})
        return result.get("deleted", {})  # type: ignore[union-attr,return-value]

    async def purge_fleet_data(self, tenant_id: str, fleet_id: str) -> dict[str, int]:
        """Hard-delete all OSS data scoped to ``(tenant_id, fleet_id)`` — the
        per-fleet analogue of ``purge_tenant_data`` for run-scoped test-tenant
        hygiene. Returns the per-table deleted counts. Idempotent at the
        storage layer — a repeat call for an already-purged fleet returns zeros.
        """
        result = await self._post("/purge/fleet-data", {"tenant_id": tenant_id, "fleet_id": fleet_id})
        return result.get("deleted", {})  # type: ignore[union-attr,return-value]

    async def count_tenant_data(self, tenant_id: str) -> dict[str, int]:
        """Per-table row count for ``tenant_id`` — drives the
        deletion-preview panel (CAURA-696). Same table set + scoping
        as ``purge_tenant_data`` so the preview is a faithful forecast.
        Read-only.
        """
        result = await self._post("/preview/tenant-counts", {"tenant_id": tenant_id})
        return result.get("counts", {})  # type: ignore[union-attr,return-value]

    async def count_active(self, tenant_id: str, fleet_id: str | None = None) -> int:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        result = await self._get("/memories/count-active", **params)
        return (result or {}).get("count", 0)

    async def count_all(self, tenant_id: str, fleet_id: str | None = None) -> int:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        result = await self._get("/memories/count", **params)
        return (result or {}).get("count", 0)

    async def count_distinct_agents(self) -> int:
        """Global count of distinct agent identities across all memories."""
        result = await self._get("/memories/distinct-agents")
        return (result or {}).get("count", 0)

    async def count_distinct_tenants(self) -> int:
        """Global count of distinct tenants with at least one live memory."""
        result = await self._get("/memories/distinct-tenants")
        return (result or {}).get("count", 0)

    async def update_embedding(
        self,
        memory_id: str,
        embedding: list[float],
    ) -> dict | None:
        return await self._patch(
            f"/memories/{memory_id}/embedding",
            {"embedding": embedding},
        )

    async def update_memory_entities(
        self,
        memory_id: str,
        entity_links: list[dict],
    ) -> dict | None:
        return await self._patch(
            f"/memories/{memory_id}/entities",
            {"entity_links": entity_links},
        )

    async def get_entity_links_for_memories(
        self,
        memory_ids: list[str],
    ) -> dict:
        return await self._post("/memories/entity-links", {"memory_ids": memory_ids}, read=True)  # type: ignore[return-value]

    async def get_memory_stats(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        return await self._get("/memories/stats", **params) or {}

    async def get_embedding_coverage(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        return await self._get("/memories/embedding-coverage", **params) or {}

    async def get_type_distribution(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        return await self._get("/memories/type-distribution", **params) or {}

    async def get_recent_memories(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        params: dict[str, Any] = {"tenant_id": tenant_id, "limit": limit}
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        return await self._get_list("/memories/recent", **params)

    async def get_lifecycle_candidates(self, tenant_id: str) -> dict:
        return await self._get("/memories/lifecycle-candidates", tenant_id=tenant_id) or {}

    async def check_near_duplicates(self, data: dict) -> dict:
        return await self._post("/memories/near-duplicates", data)  # type: ignore[return-value]

    async def find_neighbors_by_embedding(self, data: dict) -> list[dict]:
        return await self._post("/memories/neighbors-by-embedding", data) or []  # type: ignore[return-value]

    async def mark_dedup_checked(self, memory_ids: list[str]) -> dict:
        return await self._post("/memories/mark-dedup-checked", {"memory_ids": memory_ids})  # type: ignore[return-value]

    async def batch_update_status(self, data: dict) -> dict:
        return await self._post("/memories/batch-update-status", data)  # type: ignore[return-value]

    async def bulk_get_memories(
        self,
        ids: list[str],
        tenant_id: str | None = None,
    ) -> list[dict | None]:
        """Fetch many memories in one round-trip; order matches input ``ids``.

        Missing rows (deleted, nonexistent, or — when ``tenant_id`` is
        provided — cross-tenant) come back as ``None`` in the same slot
        rather than being dropped from the list. Lets callers zip the
        response back to their original id list. Capped at 1000 ids
        server-side; callers needing more must chunk client-side.
        """
        payload: dict[str, Any] = {"ids": ids}
        if tenant_id is not None:
            payload["tenant_id"] = tenant_id
        return await self._post(  # type: ignore[return-value]
            "/memories/bulk-get", payload, read=True
        )

    # =====================================================================
    # Entities
    # =====================================================================

    async def create_entity(self, data: dict) -> dict:
        return await self._post("/entities", data)  # type: ignore[return-value]

    async def get_entity(self, entity_id: str) -> dict | None:
        return await self._get(f"/entities/{entity_id}")

    async def update_entity(self, entity_id: str, data: dict) -> dict | None:
        return await self._patch(f"/entities/{entity_id}", data)

    async def find_exact_entity(
        self,
        tenant_id: str,
        name: str,
        fleet_id: str | None = None,
        entity_type: str | None = None,
    ) -> dict | None:
        params: dict[str, Any] = {"tenant_id": tenant_id, "name": name}
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        if entity_type is not None:
            params["entity_type"] = entity_type
        return await self._get("/entities/exact", **params)

    async def find_by_embedding_similarity(
        self,
        tenant_id: str,
        embedding: list[float],
        limit: int = 3,
        entity_type: str | None = None,
        fleet_id: str | None = None,
    ) -> list[dict]:
        payload: dict[str, Any] = {
            "tenant_id": tenant_id,
            "name_embedding": embedding,
            "limit": limit,
        }
        if entity_type is not None:
            payload["entity_type"] = entity_type
        if fleet_id is not None:
            payload["fleet_id"] = fleet_id
        return await self._post(  # type: ignore[return-value]
            "/entities/embedding-similarity",
            payload,
        )

    async def bulk_upsert_entities(self, items: list[dict]) -> list[dict]:
        """Apply many entity create / update operations in one round-trip.

        Companion to ``bulk_resolve_entities``. Per-item shape and
        response semantics documented at ``/entities/bulk-upsert``.
        """
        return await self._post(  # type: ignore[return-value]
            "/entities/bulk-upsert", {"items": items}
        )

    async def bulk_resolve_entities(
        self,
        tenant_id: str,
        items: list[dict],
        threshold: float,
        candidate_limit: int = 3,
    ) -> list[dict | None]:
        """Resolve many entities in one round-trip. See
        ``/entities/bulk-resolve`` for the per-item payload + response
        shape. The caller computes attribute merges from the response
        and follows up with ``bulk_upsert_entities``."""
        payload: dict[str, Any] = {
            "tenant_id": tenant_id,
            "items": items,
            "threshold": threshold,
            "candidate_limit": candidate_limit,
        }
        return await self._post(  # type: ignore[return-value]
            "/entities/bulk-resolve", payload, read=True
        )

    async def fts_search_entities(self, data: dict) -> list[str]:
        return await self._post("/entities/fts-search", data, read=True)  # type: ignore[return-value]

    async def expand_graph(self, data: dict) -> dict:
        return await self._post("/entities/expand-graph", data, read=True)  # type: ignore[return-value]

    async def get_full_graph(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
    ) -> dict:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        return await self._get("/entities/full-graph", **params) or {}

    async def list_entities(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        params: dict[str, Any] = {"tenant_id": tenant_id, "limit": limit, "offset": offset}
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        return await self._get_list("/entities", **params)

    async def count_memories_per_entity(
        self,
        tenant_id: str,
        entity_ids: list[str],
    ) -> dict:
        return await self._post(  # type: ignore[return-value]
            "/entities/count-memories",
            {"tenant_id": tenant_id, "entity_ids": entity_ids},
            read=True,
        )

    async def get_entity_with_linked_memories(self, entity_id: str) -> dict | None:
        return await self._get(f"/entities/{entity_id}/with-memories")

    async def get_outgoing_relations(self, entity_id: str) -> list[dict]:
        return await self._get_list(f"/entities/{entity_id}/relations")

    async def find_relation(
        self,
        source_id: str,
        target_id: str,
        relation_type: str,
    ) -> dict | None:
        return await self._get(
            "/entities/relations/find",
            source_id=source_id,
            target_id=target_id,
            relation_type=relation_type,
        )

    async def create_relation(self, data: dict) -> dict:
        return await self._post("/entities/relations", data)  # type: ignore[return-value]

    async def find_entity_link(self, memory_id: str, entity_id: str) -> dict | None:
        return await self._get(
            "/entities/links/find",
            memory_id=memory_id,
            entity_id=entity_id,
        )

    async def create_entity_link(self, data: dict) -> dict:
        return await self._post("/entities/links", data)  # type: ignore[return-value]

    async def bulk_upsert_entity_links(self, items: list[dict]) -> list[dict]:
        """Idempotently create many memory→entity links in one round-trip.

        Per-item: ``{"input_idx", "memory_id", "entity_id", "role"}``.
        Response aligned to input with ``{"input_idx", "memory_id",
        "entity_id", "role", "created": bool}``. ``created=False`` means
        the link already existed (its prior role preserved).
        """
        return await self._post(  # type: ignore[return-value]
            "/entities/links/bulk", {"items": items}
        )

    async def get_memory_ids_by_entity_ids(
        self,
        entity_ids: list[str],
    ) -> list[tuple]:
        result = await self._post(
            "/entities/memory-ids-by-entity-ids",
            {"entity_ids": entity_ids},
        )
        return result  # type: ignore[return-value]

    async def find_orphaned_entities(self, tenant_id: str) -> list[dict]:
        return await self._get_list("/entities/orphaned", tenant_id=tenant_id)

    async def find_broken_entity_links(self, tenant_id: str) -> list[dict]:
        return await self._get_list("/entities/broken-links", tenant_id=tenant_id)

    # =====================================================================
    # Agents
    # =====================================================================

    async def create_or_update_agent(self, data: dict) -> dict:
        return await self._post("/agents", data)  # type: ignore[return-value]

    async def get_agent(self, agent_id: str, tenant_id: str) -> dict | None:
        return await self._get(f"/agents/{agent_id}", tenant_id=tenant_id)

    async def list_agents(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        return await self._get_list("/agents", **params)

    async def update_trust_level(self, agent_id: str, data: dict) -> dict | None:
        return await self._patch(f"/agents/{agent_id}/trust-level", data)

    async def update_search_profile(self, agent_id: str, data: dict) -> dict | None:
        return await self._patch(f"/agents/{agent_id}/search-profile", data)

    async def reset_search_profile(
        self,
        agent_id: str,
        tenant_id: str,
    ) -> dict | None:
        return await self._post_optional(
            f"/agents/{agent_id}/search-profile/reset",
            {"tenant_id": tenant_id},
        )

    async def get_search_profile(
        self,
        agent_id: str,
        tenant_id: str,
    ) -> dict | None:
        return await self._get(
            f"/agents/{agent_id}/search-profile",
            tenant_id=tenant_id,
        )

    async def backfill_from_memories(self, tenant_id: str) -> dict:
        return await self._post(  # type: ignore[return-value]
            "/agents/backfill-from-memories",
            {"tenant_id": tenant_id},
        )

    async def update_agent_fleet(self, agent_id: str, data: dict) -> dict | None:
        return await self._patch(f"/agents/{agent_id}/fleet", data)

    async def delete_agent(self, agent_id: str, tenant_id: str) -> bool:
        return await self._delete(f"/agents/{agent_id}", tenant_id=tenant_id)

    # =====================================================================
    # Documents
    # =====================================================================

    async def upsert_document(self, data: dict) -> dict:
        return await self._post("/documents", data)  # type: ignore[return-value]

    async def get_document(
        self,
        tenant_id: str,
        collection: str,
        doc_id: str,
    ) -> dict | None:
        return await self._get(
            f"/documents/{collection}/{doc_id}",
            tenant_id=tenant_id,
        )

    async def query_documents(self, data: dict) -> list[dict]:
        return await self._post("/documents/query", data, read=True)  # type: ignore[return-value]

    async def list_documents(
        self,
        tenant_id: str,
        collection: str,
        fleet_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        params: dict[str, Any] = {"tenant_id": tenant_id, "limit": limit, "offset": offset}
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        return await self._get_list(f"/documents/{collection}", **params)

    async def delete_document(
        self,
        tenant_id: str,
        collection: str,
        doc_id: str,
    ) -> bool:
        return await self._delete(
            f"/documents/{collection}/{doc_id}",
            tenant_id=tenant_id,
        )

    # =====================================================================
    # Keystones (CAURA-000)
    # =====================================================================
    #
    # Thin proxies over the core-storage ``/keystones`` endpoints. The
    # GET path returns a ``(rows, truncated)`` tuple so the upstream
    # caller can surface the ``X-Truncated`` header to MCP/REST clients
    # — silent truncation hides governance gaps.

    async def list_keystones(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
        agent_id: str | None = None,
    ) -> tuple[list[dict], bool]:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        if agent_id is not None:
            params["agent_id"] = agent_id
        headers = await self._auth_headers(read=True)

        def _do() -> Awaitable[httpx.Response]:
            return self._read_http.get(
                f"{self._read_prefix}/keystones",
                params=params,
                headers=headers,
            )

        resp = await self._execute(_do, retry=with_retry, label="GET /keystones")
        self._maybe_evict_on_auth_error(resp, read=True)
        resp.raise_for_status()
        truncated = resp.headers.get("X-Truncated", "").lower() == "true"
        return resp.json(), truncated

    async def upsert_keystone(self, data: KeystoneUpsertPayload) -> dict:
        return await self._post("/keystones", data)  # type: ignore[return-value]

    async def delete_keystone(self, tenant_id: str, doc_id: str) -> bool:
        return await self._delete(f"/keystones/{doc_id}", tenant_id=tenant_id)

    # =====================================================================
    # Fleet
    # =====================================================================

    async def upsert_node(self, data: dict) -> dict:
        return await self._post("/fleet/nodes", data)  # type: ignore[return-value]

    async def list_nodes(self, tenant_id: str, fleet_id: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        return await self._get_list("/fleet/nodes", **params)

    async def fleet_stats(self, tenant_id: str, fleet_id: str | None = None) -> dict:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        if fleet_id:
            params["fleet_id"] = fleet_id
        return await self._get("/fleet/stats", **params) or {}

    async def get_node(self, tenant_id: str, node_name: str) -> dict | None:
        return await self._get(f"/fleet/nodes/{node_name}", tenant_id=tenant_id)

    async def delete_node(self, tenant_id: str, node_name: str) -> bool:
        return await self._delete(f"/fleet/nodes/{node_name}", tenant_id=tenant_id)

    async def create_command(self, data: dict) -> dict:
        return await self._post("/fleet/commands", data)  # type: ignore[return-value]

    async def list_commands(
        self,
        tenant_id: str,
        node_name: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        if node_name is not None:
            params["node_name"] = node_name
        if status is not None:
            params["status"] = status
        return await self._get_list("/fleet/commands", **params)

    async def update_command_status(
        self,
        command_id: str,
        data: dict,
    ) -> dict | None:
        return await self._patch(f"/fleet/commands/{command_id}/status", data)

    async def get_pending_commands(
        self,
        tenant_id: str,
        node_name: str,
    ) -> list[dict]:
        return await self._get_list(
            "/fleet/commands/pending",
            tenant_id=tenant_id,
            node_name=node_name,
        )

    async def ack_commands(self, command_ids: list[str]) -> dict:
        return await self._post("/fleet/commands/ack", {"command_ids": command_ids})  # type: ignore[return-value]

    async def fleet_exists(self, tenant_id: str, fleet_id: str) -> bool:
        result = await self._get(
            "/fleet/exists",
            tenant_id=tenant_id,
            fleet_id=fleet_id,
        )
        return bool(result and result.get("exists"))

    async def list_fleets(self, tenant_id: str) -> list[dict]:
        return await self._get_list("/fleet", tenant_id=tenant_id)

    async def count_nodes(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
    ) -> int:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        result = await self._get("/fleet/nodes/count", **params)
        return (result or {}).get("count", 0)

    async def delete_fleet(self, tenant_id: str, fleet_id: str) -> bool:
        return await self._delete(f"/fleet/{fleet_id}", tenant_id=tenant_id)

    # =====================================================================
    # Idempotency inbox
    # =====================================================================

    async def get_idempotency(
        self,
        tenant_id: str,
        idempotency_key: str,
    ) -> dict | None:
        # Read-before-write guard: if the replica lags, a retry that
        # should replay the cached response would instead re-run the
        # operation. Must hit the writer so we see the row the previous
        # attempt committed.
        return await self._get(
            "/idempotency",
            read=False,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
        )

    async def claim_idempotency(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        request_hash: str,
        expires_at: str,
    ) -> tuple[bool, dict | None]:
        """Try to claim ``(tenant_id, idempotency_key)``.

        Returns ``(True, row)`` if the caller won the race and should
        proceed with the handler. Returns ``(False, existing_row_or_None)``
        when another caller already holds the key — caller polls
        :meth:`get_idempotency` until the existing row's ``is_pending``
        flips to False. Existing row may be ``None`` if it expired
        between the conflicting INSERT and our follow-up SELECT.
        """
        headers = await self._auth_headers(read=False)

        def _do() -> Awaitable[httpx.Response]:
            return self._http.post(
                f"{self._prefix}/idempotency/claim",
                json={
                    "tenant_id": tenant_id,
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "expires_at": expires_at,
                },
                headers=headers,
            )

        resp = await self._execute(_do, retry=with_connect_phase_retry, label="POST /idempotency/claim")
        self._maybe_evict_on_auth_error(resp, read=False)
        if resp.status_code == 201:
            return True, resp.json()
        if resp.status_code == 409:
            body = resp.json()
            # Storage-api signals row presence via an explicit ``found``
            # field on the 409 body so we don't infer it from incidental
            # keys (the previous ``"tenant_id" in body`` check would
            # silently break the moment the error body gained a
            # tenant_id field). ``found is False`` only on the
            # vanished-row branch; a real conflicting row sets
            # ``found: True`` alongside the row payload.
            return False, body if body.get("found") is not False else None
        # Any status not caught above propagates as ``HTTPStatusError``.
        resp.raise_for_status()
        # Unreachable — included so mypy sees a return path matching
        # the declared ``tuple[bool, dict | None]`` signature.
        return False, None  # pragma: no cover

    async def upsert_idempotency(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
        request_hash: str,
        response_body: dict,
        status_code: int,
        expires_at: str,
    ) -> dict:
        return await self._post(  # type: ignore[return-value]
            "/idempotency",
            {
                "tenant_id": tenant_id,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "response_body": response_body,
                "status_code": status_code,
                "expires_at": expires_at,
            },
        )

    # =====================================================================
    # Audit
    # =====================================================================

    async def create_audit_log(self, data: dict) -> dict:
        return await self._post("/audit-logs", data)  # type: ignore[return-value]

    async def create_audit_logs_bulk(self, events: list[dict]) -> dict:
        """Batched audit insert (CAURA-628). One HTTP POST + one
        multi-row INSERT regardless of batch size, vs N round-trips +
        N table-lock acquisitions on the legacy single-event path.

        ``idempotent=True``: each event carries a ``client_event_id`` (minted in
        ``log_action``) and storage dedups on it under the per-tenant chain-head
        lock, so a retry on ReadTimeout / 5xx re-sends the same ids and any
        already-committed events are skipped — no double-append to the
        tamper-evident chain. This recovers the lost-ack case that previously
        dropped a whole tenant's audit slice (connect-phase-only retry)."""
        return await self._post(  # type: ignore[return-value]
            "/audit-logs/bulk", {"events": events}, idempotent=True
        )

    async def list_audit_logs(
        self,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
        action: str | None = None,
        resource_type: str | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "limit": limit,
            "offset": offset,
        }
        if action is not None:
            params["action"] = action
        if resource_type is not None:
            params["resource_type"] = resource_type
        return await self._get_list("/audit-logs", **params)

    async def verify_audit_chain(self, tenant_id: str, limit: int = 100_000) -> dict:
        """Verify a tenant's tamper-evident audit hash chain.

        Returns ``{valid, verified_count, head_seq}`` (or ``first_broken``
        on a detected break). Used by the enterprise governance UI's
        "chain intact" check.
        """
        result = await self._get("/audit-logs/verify", tenant_id=tenant_id, limit=limit)
        # Propagate failures: a None here means a network/5xx error. Returning
        # {} would hand callers a dict with no "valid" key, turning the real
        # error into a confusing KeyError downstream.
        if result is None:
            raise RuntimeError("verify_audit_chain: empty response from storage service")
        return result

    # =====================================================================
    # Lifecycle audit (CAURA-655)
    # =====================================================================

    async def create_lifecycle_audit_row(
        self,
        *,
        org_id: str,
        action: str,
        triggered_by: str,
    ) -> int:
        """Pre-publish a ``pending`` audit row for one Pub/Sub message.

        The fanout endpoint calls this immediately before each per-org
        publish so the consumer has a stable id to finalise.
        """
        result = await self._post(
            "/lifecycle-audit",
            {"org_id": org_id, "action": action, "triggered_by": triggered_by},
        )
        return result["audit_id"]  # type: ignore[index]

    async def update_lifecycle_audit_row(
        self,
        audit_id: int,
        *,
        status: str,
        stats: dict | None = None,
        error_message: str | None = None,
    ) -> None:
        """Flip the row to ``in_progress``, ``success``, or ``failure``."""
        body: dict[str, Any] = {"status": status}
        if stats is not None:
            body["stats"] = stats
        if error_message is not None:
            body["error_message"] = error_message
        await self._patch(f"/lifecycle-audit/{audit_id}", body)

    async def has_recent_lifecycle_success(
        self,
        *,
        org_id: str,
        action: str,
        since_hours: int,
    ) -> bool:
        """CAURA-657 dedup gate. The pipeline-op consumers (crystallize,
        entity-link) check this before invoking the primitive — skip the
        run when a recent successful audit row exists for the same
        org+action.
        """
        result = await self._get(
            "/lifecycle-audit/has-recent-success",
            org_id=org_id,
            action=action,
            since_hours=since_hours,
        )
        return bool((result or {}).get("has_recent_success"))

    # =====================================================================
    # Reports
    # =====================================================================

    async def create_report(self, data: dict) -> dict:
        return await self._post("/reports", data)  # type: ignore[return-value]

    async def get_report(self, report_id: str) -> dict | None:
        return await self._get(f"/reports/{report_id}")

    async def update_report(self, report_id: str, data: dict) -> dict | None:
        return await self._patch(f"/reports/{report_id}", data)

    async def find_running_report(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
        report_type: str | None = None,
    ) -> dict | None:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        if report_type is not None:
            params["report_type"] = report_type
        return await self._get("/reports/running", **params)

    async def get_latest_report(
        self,
        tenant_id: str,
        fleet_id: str | None = None,
        report_type: str | None = None,
    ) -> dict | None:
        params: dict[str, Any] = {"tenant_id": tenant_id}
        if fleet_id is not None:
            params["fleet_id"] = fleet_id
        if report_type is not None:
            params["report_type"] = report_type
        return await self._get("/reports/latest", **params)

    async def list_reports(self, tenant_id: str) -> list[dict]:
        return await self._get_list("/reports", tenant_id=tenant_id)

    # =====================================================================
    # Tasks
    # =====================================================================

    async def add_task_failure(self, data: dict) -> dict:
        return await self._post("/tasks/failures", data)  # type: ignore[return-value]

    # =====================================================================
    # Tenant suppression (CAURA-694)
    # =====================================================================

    async def is_tenant_suppressed(self, tenant_id: str) -> bool:
        """Boundary-guard read for the auth layer.

        Hot path: called on every authenticated request (behind a small
        in-process TTL cache in ``core_api.suppression``). The storage
        endpoint returns ``{tenant_id, is_suppressed}``; a missing /
        unknown tenant is the same as ``False`` (live), which matches
        the pure-OSS shape where the table is empty.

        Re-raises on transport failure rather than failing open here —
        the caller decides whether the open-fail-or-block trade-off is
        appropriate. The boundary cache currently fails open with a
        warning (preserve uptime over hardening) but a different
        caller (e.g. an admin pre-check) might want the raise.
        """
        result = await self._get(f"/tenant-suppression/{tenant_id}")
        if result is None:
            # Storage routes return 200 with ``is_suppressed: False`` for
            # unknown tenants, so ``None`` here means a 404 only the
            # ``_get`` wrapper recognises — treat it as ``live`` rather
            # than blocking, so a bad URL doesn't cause a global outage.
            return False
        return bool(result.get("is_suppressed", False))
