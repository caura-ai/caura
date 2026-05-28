"""Async-embed and async-enrich event consumers.

Subscribes to:

* ``Topics.Memory.EMBED_REQUESTED`` (CAURA-594) — one
  :class:`~common.events.memory_embed_request.MemoryEmbedRequest`,
  resolves the embedding via the platform-tier provider, PATCHes it
  back onto the memory row.
* ``Topics.Memory.ENRICH_REQUESTED`` (CAURA-595) — one
  :class:`~common.events.memory_enrich_request.MemoryEnrichRequest`,
  reconstructs the tenant config from the payload, runs
  :func:`common.enrichment.enrich_memory` (3-tier provider fallback),
  and PATCHes the resulting fields back onto the memory row.

Embed-request flow (existing):

1. Validate the envelope's payload as ``MemoryEmbedRequest``.
2. Cache lookup: if any other row in the tenant already has an
   embedding for the same ``content_hash``, reuse it and skip the
   provider call.
3. Otherwise call the platform-tier embedding provider via
   :func:`common.embedding.get_platform_embedding`.
4. PATCH the embedding back onto the memory row via core-storage-api.

Idempotency: at-least-once delivery from Pub/Sub means a redelivered
event re-PATCHes the row with the (deterministic) same embedding.
That's safe — the storage update is a write-after-write of the same
column. ``content_hash`` cache lookup keeps the redelivery cost down
to a single GET in the steady-state.

Failure modes:
* ``ValidationError`` on a bad payload → ack + drop (poison-message
  guard; matches the platform-audit-api pattern). The DLQ provisioned
  by the bootstrap script catches anything we miss.
* ``get_platform_embedding() is None`` (PLATFORM_EMBEDDING_* not
  configured) → log + ack-drop. The worker is platform-only by
  design; without the singleton there's nothing it can do, and
  raising would just hot-loop redeliveries.
* Provider exception or storage PATCH failure → raise → nack →
  Pub/Sub redelivers (subject to max-delivery-attempts → DLQ).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace

import httpx
from pydantic import ValidationError

from common.embedding import get_platform_embedding
from common.enrichment import EnrichmentResult, enrich_memory
from common.events.base import Event
from common.events.factory import get_event_bus
from common.events.memory_embed_request import MemoryEmbedRequest
from common.events.memory_embedded_publisher import publish_memory_embedded
from common.events.memory_enrich_request import MemoryEnrichRequest
from common.events.memory_enriched_publisher import publish_memory_enriched
from common.events.suppression_handlers import (
    SuppressionStorageAdapter,
    register_suppression_consumer,
)
from common.events.topics import Topics
from core_worker.clients.storage_client import (
    find_embedding_by_content_hash,
    update_memory_embedding,
    update_memory_enrichment,
    upsert_tenant_suppression,
)
from core_worker.per_tenant_concurrency import per_tenant_storage_slot

logger = logging.getLogger(__name__)

# Zero-arg factory so the consumer doesn't have to plumb Settings on
# every event. The storage_client module reads its own Settings on
# first call (singleton). Tests inject a stub by overriding this
# variable directly via ``configure``.
StorageClientFactory = Callable[[], httpx.AsyncClient]

_storage_client_factory: StorageClientFactory | None = None


def configure(storage_client_factory: StorageClientFactory) -> None:
    """Bind the per-process state the consumer needs.

    Called once at app startup before ``register_consumers`` /
    ``bus.start()``. ``storage_client_factory`` is a zero-arg callable
    returning the shared httpx client (so tests can inject a stub).
    """
    global _storage_client_factory
    _storage_client_factory = storage_client_factory


async def handle_embed_request(event: Event) -> None:
    """Process a single embed-request event."""
    try:
        request = MemoryEmbedRequest(**event.payload)
    except ValidationError:
        # Schema-drift / malformed payload. Log loudly with the
        # ``dropped=True`` alert hook (matches platform-audit-api) and
        # ack-drop so the subscription doesn't loop on a poison message.
        logger.exception(
            "dropping malformed embed-request payload",
            extra={
                "event_type": event.event_type,
                "event_id": str(event.event_id),
                "dropped": True,
            },
        )
        return

    if _storage_client_factory is None:
        # Startup ordering bug — configure() was never called. Drop with
        # a loud log (raising would nack-loop at full Pub/Sub throughput
        # against a permanently misconfigured pod).
        logger.error(
            "dropping embed-request: consumer not configured",
            extra={
                "event_id": str(event.event_id),
                "memory_id": str(request.memory_id),
                "dropped": True,
            },
        )
        return

    provider = get_platform_embedding()
    if provider is None:
        logger.error(
            "dropping embed-request: PLATFORM_EMBEDDING_* unset, no platform embedding singleton configured",
            extra={
                "event_id": str(event.event_id),
                "memory_id": str(request.memory_id),
                "dropped": True,
            },
        )
        return

    storage = _storage_client_factory()

    embedding: list[float] | None = None

    # Step 1 — content-hash cache lookup. Saves one provider call on
    # redeliveries and on legitimate within-tenant duplicates.
    if request.content_hash:
        embedding = await find_embedding_by_content_hash(
            storage,
            tenant_id=request.tenant_id,
            content_hash=request.content_hash,
        )
        if embedding is not None:
            logger.info(
                "reusing cached embedding for content_hash",
                extra={
                    "memory_id": str(request.memory_id),
                    "tenant_id": request.tenant_id,
                },
            )

    # Step 2 — provider call (skipped on cache hit).
    if embedding is None:
        embedding = await provider.embed(request.content)

    # Step 3 — persist. Raises on non-2xx → nacks → redelivers.
    # Per-tenant slot scoped to the PATCH roundtrip only, so a
    # tenant-A storm can't park every storage-writer connection here
    # while tenant B's lone PATCH queues behind. Provider call above
    # stays unguarded — that's the expensive step and it doesn't
    # touch the storage-writer pool.
    async with per_tenant_storage_slot(request.tenant_id):
        await update_memory_embedding(
            storage,
            memory_id=request.memory_id,
            tenant_id=request.tenant_id,
            embedding=embedding,
        )

    # Back-channel: announce successful embed so core-api can fire
    # post-embed contradiction detection. Closes the documented
    # ``handle_memory_enriched`` gap that silently dropped detection
    # whenever enrichment landed before embedding (the only case under
    # ``EMBED_ON_HOT_PATH=false``). Best-effort — a publish failure
    # MUST NOT nack the upstream PATCH that already succeeded; the
    # embedding is durable in storage either way.
    try:
        await publish_memory_embedded(
            memory_id=request.memory_id,
            tenant_id=request.tenant_id,
            content=request.content,
        )
    except Exception:
        logger.exception(
            "embedded back-channel publish failed; ack-continuing",
            extra={
                "memory_id": str(request.memory_id),
                "tenant_id": request.tenant_id,
            },
        )

    logger.info(
        "embed-request processed",
        extra={
            "memory_id": str(request.memory_id),
            "tenant_id": request.tenant_id,
            "provider": provider.provider_name,
            "model": provider.model,
        },
    )


# ---------------------------------------------------------------------------
# Enrich-request consumer (CAURA-595)
# ---------------------------------------------------------------------------


# ORM column fields the worker writes directly. Stays in sync with the
# corresponding ``Memory`` columns + the synchronous-path mapping in
# ``core_api.services.memory_service``. Anything an ``EnrichmentResult``
# field would write to but isn't here lands in ``metadata_patch`` below.
_ENRICHMENT_ORM_FIELDS: frozenset[str] = frozenset(
    {
        "memory_type",
        "weight",
        "title",
        "status",
        "ts_valid_start",
        "ts_valid_end",
    }
)

# Enrichment fields that live in ``Memory.metadata`` JSONB. Synchronous
# path (``memory_service.py``) writes them as ``metadata["summary"]``,
# ``metadata["tags"]``, etc.; the worker mirrors that via the storage
# layer's atomic ``metadata_patch`` JSONB merge.
_ENRICHMENT_METADATA_FIELDS: frozenset[str] = frozenset(
    {
        "summary",
        "tags",
        "contains_pii",
        "pii_types",
        "retrieval_hint",
        "llm_ms",
    }
)

# ``EnrichmentResult`` fields the worker INTENTIONALLY does not route
# to storage. Listed explicitly so the exhaustive-routing assert below
# fails when a future field is added without a code site to handle it
# — silent drops are the failure mode we're guarding against.
#
# ``atomic_facts``: the synchronous write path fans these out into
# child memories; the async worker doesn't yet implement that — see
# the ``logger.debug`` in ``handle_enrich_request`` that surfaces the
# gap when an enrichment produces them.
_ENRICHMENT_UNROUTED_FIELDS: frozenset[str] = frozenset({"atomic_facts"})

# Metadata fields that ALWAYS overwrite (when not ``None`` and the
# result came from a real LLM call — see the ``llm_ms > 0`` guard at
# the call site) so a re-delivery / second-LLM-run can clear stale
# state from an earlier write. JSONB ``||`` merge treats ``False`` /
# ``""`` / ``[]`` as ordinary values and overwrites keyed entries.
#
# * ``contains_pii`` / ``pii_types`` — non-deterministic LLM output;
#   an earlier ``contains_pii=True`` must be clearable.
# * ``retrieval_hint`` — the prompt explicitly instructs the LLM to
#   return ``""`` for content that's already query-aligned; without
#   always-write a re-enrichment that drops the hint would leave
#   metadata stale AND the embedding still prefix-augmented.
# * ``tags`` — the prompt instructs the LLM to always populate 2-6
#   tags. ``tags=[]`` from a real LLM run means "no tags" intentionally
#   and must overwrite a prior non-empty list.
# * ``summary`` — without the guard, a heuristic-fallback redelivery
#   would write ``fake_enrich``'s ``summary=content[:200]`` over a
#   prior quality LLM summary. The truncation is a non-empty string,
#   so the ``value in (False, 0, "", [])`` skip-check below doesn't
#   catch it. Pairing summary with the ``llm_ms > 0`` guard ensures
#   only real-LLM summaries can overwrite (or clear, if the LLM
#   returns ``""``).
_ENRICHMENT_ALWAYS_WRITE_METADATA: frozenset[str] = frozenset(
    {"contains_pii", "pii_types", "retrieval_hint", "summary", "tags"}
)

# Defence-in-depth: a typo in the tuples above would silently drop in
# the storage layer's ``hasattr(Memory, key)`` filter, *and* a future
# ``EnrichmentResult`` field that no one wires to a routing tuple
# would silently fall on the floor. Catch both at module import.
_unrouted = (
    set(EnrichmentResult.model_fields)
    - _ENRICHMENT_ORM_FIELDS
    - _ENRICHMENT_METADATA_FIELDS
    - _ENRICHMENT_UNROUTED_FIELDS
)
# Raise rather than ``assert`` — ``python -O`` strips assertions, and
# this guard's whole point is to surface routing-table drift on every
# import in every environment. Matches
# ``_assert_override_fields_match_schemas`` in ``memory_service.py``.
if _unrouted:
    raise RuntimeError(
        f"EnrichmentResult fields not routed to storage by handle_enrich_request: "
        f"{sorted(_unrouted)} — add to one of _ENRICHMENT_ORM_FIELDS / "
        f"_ENRICHMENT_METADATA_FIELDS / _ENRICHMENT_UNROUTED_FIELDS"
    )
del _unrouted


def _build_tenant_config(request: MemoryEnrichRequest) -> SimpleNamespace | None:
    """Reconstruct a duck-typed tenant_config from the event payload.

    ``common.enrichment.service.enrich_memory`` only reads attributes
    off ``tenant_config``; a ``SimpleNamespace`` matching that surface is
    enough — no need to import ``ResolvedConfig``.

    Returns ``None`` only when the payload genuinely carries no
    tenant-specific config — neither a provider preference nor any API
    keys. The early-return on ``enrichment_provider is None`` alone
    would silently discard a tenant's keys, making the async path
    behave differently from the sync path: sync sees
    ``provider=None`` and defaults to ``"openai"`` while still using
    the tenant's ``openai_api_key``; async would fall through to the
    platform-tier provider or the heuristic. The check below mirrors
    sync semantics — if any credential is present we build the
    namespace with ``enrichment_provider=None`` and let
    :func:`enrich_memory` apply its ``or ProviderName.OPENAI``
    default.
    """
    has_any_credentials = any(
        (
            request.openai_api_key,
            request.anthropic_api_key,
            request.openrouter_api_key,
            request.gemini_api_key,
        )
    )
    if request.enrichment_provider is None and not has_any_credentials:
        return None

    fb_provider = request.fallback_provider
    fb_model = request.fallback_model

    def resolve_fallback() -> tuple[str | None, str | None]:
        return fb_provider, fb_model

    return SimpleNamespace(
        enrichment_provider=request.enrichment_provider,
        enrichment_model=request.enrichment_model,
        openai_api_key=request.openai_api_key,
        anthropic_api_key=request.anthropic_api_key,
        openrouter_api_key=request.openrouter_api_key,
        gemini_api_key=request.gemini_api_key,
        # Synthesised callable matches the
        # ``ResolvedConfig.resolve_fallback()`` shape ``call_with_fallback``
        # expects. The publisher already pre-resolved the tuple at
        # publish time so the worker doesn't run the policy itself.
        resolve_fallback=resolve_fallback,
    )


def _build_patch(
    result: EnrichmentResult,
    agent_provided_fields: list[str] | None = None,
) -> dict:
    """Translate an ``EnrichmentResult`` into the storage PATCH body.

    ORM-column fields go at the top level; metadata fields land under
    ``metadata_patch`` for the storage layer's atomic JSONB merge.

    ``agent_provided_fields`` is the publisher's snapshot of which
    columns the agent set explicitly at write time. Those fields are
    excluded from the PATCH so a redelivery (or a slow worker run)
    can't downgrade an agent-provided ``weight=0.9`` back to
    ``EnrichmentResult.weight``'s default of ``0.7``. Critical because
    Pydantic defaults survive ``exclude_none=True`` — every event
    technically carries values for ``memory_type``, ``weight``,
    ``status``.

    ``ts_valid_*`` are emitted as ISO strings (``model_dump(mode="json")``).
    asyncpg requires datetime instances for ``DateTime(timezone=True)``
    columns and rejects ISO strings with ``CannotCoerceError``, so the
    storage layer's ``PATCH /memories/{id}`` route calls
    ``_parse_datetimes(body)`` at the API boundary to coerce these
    fields before the SQL UPDATE runs (mirrors the POST route's
    pre-existing parse step). Keeping the worker's payload as ISO
    strings keeps it JSON-serialisable end-to-end (httpx ``json=``
    falls back to stdlib ``json.dumps`` which can't serialise
    ``datetime``); the coercion lives at the storage boundary
    rather than per-publisher. This was a real production bug
    until 2026-04-26 — see PR <CAURA-595-ts-valid-iso-coercion>.
    """
    skip = frozenset(agent_provided_fields or ())
    dump = result.model_dump(mode="json", exclude_none=True)

    # ``ts_valid_*`` are semantically meaningful as ``None`` ("this
    # memory has no validity bounds"), distinct from "field absent".
    # ``exclude_none=True`` would silently drop them, leaving stale
    # dates from a prior enrichment run intact in storage. Preserve
    # them explicitly so a re-delivery (or a different LLM run that
    # doesn't infer dates) can clear what an earlier run wrote.
    #
    # GUARD: only null-patch when the result came from a real LLM
    # call (``llm_ms > 0``). The heuristic fallback (``fake_enrich``
    # — used when the configured provider fails after retries +
    # fallback chain exhausts) NEVER infers dates, so its absence of
    # ``ts_valid_*`` is "I don't know", not "no bounds". Without this
    # guard, a Pub/Sub redelivery that coincides with an LLM outage
    # would land on the heuristic and wipe temporal bounds a prior
    # successful run wrote — silent data loss with no log surface.
    if result.llm_ms > 0:
        for ts_field in ("ts_valid_start", "ts_valid_end"):
            if ts_field not in skip and ts_field not in dump:
                dump[ts_field] = None

    patch: dict = {}
    for field in _ENRICHMENT_ORM_FIELDS:
        if field in skip:
            continue
        if field in dump:
            value = dump[field]
            # Empty title from the LLM (or from ``fake_enrich`` for
            # very short content) shouldn't clobber a previously-
            # stored valid title. Other ORM fields (``memory_type``,
            # ``weight``, ``status``) have schema defaults that
            # ``EnrichmentResult`` always emits — those go through
            # ``agent_provided_fields`` for the gate. ``title`` has
            # no agent-side counterpart (it's enricher-only output)
            # and no useful default — empty == "didn't infer".
            if field == "title" and value == "":
                continue
            patch[field] = value

    # The hot-path writer set ``metadata.enrichment_pending=true`` when
    # deferring; clear it unconditionally on every successful worker
    # PATCH so a read-after-success returns clean state. Pre-seeds the
    # patch dict so the ``if metadata_patch:`` gate below always fires
    # (when heuristic fallback produces no real metadata fields, this
    # seed ensures the PATCH still goes out and clears the stale flag
    # — without it, a fallback redelivery would silently keep the row
    # marked pending forever). Storage's JSONB ``||`` merge overwrites
    # a prior ``True`` with ``False`` cleanly.
    metadata_patch: dict = {"enrichment_pending": False}
    for field in _ENRICHMENT_METADATA_FIELDS:
        if field in skip:
            continue
        value = dump.get(field)
        if field in _ENRICHMENT_ALWAYS_WRITE_METADATA:
            # Same ``llm_ms > 0`` guard as ``ts_valid_*`` above:
            # ``fake_enrich`` returns ``contains_pii=False``,
            # ``pii_types=[]``, ``tags=[]``, ``retrieval_hint=""`` as
            # Pydantic defaults — all non-None. Without this guard, a
            # heuristic-fallback redelivery (LLM outage during a
            # storage 5xx retry) would clobber real LLM-produced PII
            # detection + recall hints + tags that an earlier
            # successful run wrote. The ``llm_ms > 0`` proxy is the
            # same one ``ts_valid_*`` uses; ``fake_enrich`` hard-codes
            # ``llm_ms=0`` so the proxy is reliable.
            if result.llm_ms > 0 and value is not None:
                metadata_patch[field] = value
            continue
        # Other metadata fields: drop the specific defaults that carry
        # no information — heuristic-fallback ``"summary": ""``,
        # ``"llm_ms": 0`` from early-return paths. Listed by value
        # rather than a blanket ``if not value`` so a future
        # ``dict``-typed field isn't accidentally silenced.
        if value is None or value in (False, 0, "", []):
            continue
        metadata_patch[field] = value
    if metadata_patch:
        patch["metadata_patch"] = metadata_patch

    return patch


async def handle_enrich_request(event: Event) -> None:
    """Process a single enrich-request event.

    Mirrors :func:`handle_embed_request`'s shape: validate → reconstruct
    tenant config → run enricher (3-tier fallback inside
    ``common.enrichment.enrich_memory``) → PATCH result onto the row.
    """
    try:
        request = MemoryEnrichRequest(**event.payload)
    except ValidationError:
        logger.exception(
            "dropping malformed enrich-request payload",
            extra={
                "event_type": event.event_type,
                "event_id": str(event.event_id),
                "dropped": True,
            },
        )
        return

    if _storage_client_factory is None:
        logger.error(
            "dropping enrich-request: consumer not configured",
            extra={
                "event_id": str(event.event_id),
                "memory_id": str(request.memory_id),
                "dropped": True,
            },
        )
        return

    storage = _storage_client_factory()
    tenant_config = _build_tenant_config(request)

    reference_dt = datetime.fromisoformat(request.reference_datetime) if request.reference_datetime else None

    # ``enrich_memory`` never raises — it falls through to the keyword
    # heuristic (``fake_enrich``) on every error path. So a transient
    # provider failure can't nack-loop the subscription.
    result = await enrich_memory(
        request.content,
        tenant_config,
        reference_datetime=reference_dt,
    )

    patch = _build_patch(result, request.agent_provided_fields)

    # Surface the sync-vs-async behavioural gap: the synchronous write
    # path in ``memory_service.py`` fans ``atomic_facts`` out into
    # child memories. The async worker doesn't yet — content with
    # multiple distinct claims gets fewer memories on this path.
    # Implementing child-memory creation in the worker requires a
    # fresh embed roundtrip per child + a parent-link plumb through
    # storage; tracked separately.
    #
    # Logged at WARNING (not ERROR): the gap is real but expected
    # under the current PR scope, and ERROR-paging on every multi-fact
    # write would drown the on-call alert channel without giving them
    # an actionable fix. The follow-up ticket reference makes the gap
    # discoverable; a dashboard counter on the WARNING string lets
    # operators quantify exposure.
    if result.atomic_facts:
        logger.warning(
            "enrich-request for memory %s produced %d atomic_facts; "
            "child-memory creation not yet implemented in async path "
            "(tracked: CAURA-595 follow-up) — secondary facts will "
            "NOT appear as child memories",
            request.memory_id,
            len(result.atomic_facts),
        )

    # Per-tenant slot scoped to the PATCH only; matches the embed
    # consumer above. The LLM enrichment call upstream is the
    # expensive step but doesn't touch the storage-writer pool.
    async with per_tenant_storage_slot(request.tenant_id):
        await update_memory_enrichment(
            storage,
            memory_id=request.memory_id,
            tenant_id=request.tenant_id,
            fields=patch,
        )

    # Back-channel: announce successful enrichment so core-api (or any
    # other subscriber) can react. Best-effort — a publish failure
    # MUST NOT nack the upstream PATCH that already succeeded; the
    # enrichment is durable in storage either way. ``logger.exception``
    # captures the failure for alerting; the consumer returns
    # successfully so Pub/Sub acks the input event.
    try:
        await publish_memory_enriched(
            memory_id=request.memory_id,
            tenant_id=request.tenant_id,
            content=request.content,
            retrieval_hint=result.retrieval_hint or "",
        )
    except Exception:
        logger.exception(
            "enriched back-channel publish failed; ack-continuing",
            extra={
                "memory_id": str(request.memory_id),
                "tenant_id": request.tenant_id,
            },
        )

    logger.info(
        "enrich-request processed",
        extra={
            "memory_id": str(request.memory_id),
            "tenant_id": request.tenant_id,
            "provider": request.enrichment_provider or "platform",
            "memory_type": result.memory_type,
            "llm_ms": result.llm_ms,
        },
    )


class _SuppressionAdapter(SuppressionStorageAdapter):
    """Thin adapter wiring :func:`upsert_tenant_suppression` into the
    shared :class:`SuppressionStorageAdapter` contract (CAURA-694).

    Lives in the consumer module rather than ``clients/`` so the lazy
    storage-client lookup (``_storage_client_factory``) stays bound to
    the consumer's ``configure`` entry point — same pattern the embed
    + enrich handlers use to reach the storage client.
    """

    async def set_tenant_suppression(self, *, tenant_id: str, action: str, updated_by: str | None) -> None:
        if _storage_client_factory is None:
            raise RuntimeError("consumer.configure() must run before register_consumers()")
        client = _storage_client_factory()
        await upsert_tenant_suppression(
            client,
            tenant_id=tenant_id,
            action=action,
            updated_by=updated_by,
        )


def register_consumers() -> None:
    """Wire the consumers into the event bus.

    Called once at app startup, before ``bus.start()`` — the Pub/Sub
    backend spawns its pull loops in ``start()`` based on the current
    handler registry, so a late ``subscribe()`` would silently orphan
    the handler.
    """
    bus = get_event_bus()
    bus.subscribe(Topics.Memory.EMBED_REQUESTED, handle_embed_request)
    bus.subscribe(Topics.Memory.ENRICH_REQUESTED, handle_enrich_request)
    # CAURA-694: register the org-suppression mirror handler. Subscribing
    # here keeps the registration order single-file rather than scattered
    # across multiple ``register_*`` entry points.
    register_suppression_consumer(_SuppressionAdapter())
