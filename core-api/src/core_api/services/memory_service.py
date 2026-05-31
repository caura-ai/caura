import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from core_api.clients.storage_client import get_storage_client
from core_api.config import settings
from core_api.middleware.per_tenant_concurrency import per_tenant_storage_slot
from core_api.tasks import track_task

try:
    from openai import OpenAIError
except ImportError:

    class OpenAIError(Exception):
        pass  # type: ignore[misc]


try:
    from google.api_core.exceptions import GoogleAPIError
except ImportError:

    class GoogleAPIError(Exception):
        pass  # type: ignore[misc]


from common.constants import VECTOR_DIM
from common.embedding import get_embedding, get_embeddings_batch, get_query_embedding
from common.events import publish_memory_embed_request, publish_memory_enrich_request
from core_api.constants import (
    BULK_EMBEDDING_TIMEOUT_SECONDS,
    BULK_ENRICHMENT_CONCURRENCY,
    BULK_ENRICHMENT_TOTAL_TIMEOUT_SECONDS,
    CHUNKING_THRESHOLD_CHARS,
    CRYSTALLIZER_SHORT_CONTENT_CHARS,
    DEFAULT_MEMORY_WEIGHT,
    DEFAULT_SEARCH_TOP_K,
    EMBEDDING_CACHE_TTL,
    FRESHNESS_DECAY_DAYS,
    FRESHNESS_FLOOR,
    FTS_BOOST_MAX_TOKENS,
    FTS_BOOST_SPECIFICITY_RATIO,
    FTS_WEIGHT,
    FTS_WEIGHT_BOOSTED,
    GRAPH_HOP_BOOST,
    GRAPH_MAX_BOOSTED_MEMORIES,
    GRAPH_MAX_HOPS,
    MIN_SEARCH_SIMILARITY,
    OPENAI_EMBEDDING_MODEL,
    RECALL_BOOST_CAP,
    RECALL_DECAY_WINDOW_DAYS,
    SEARCH_OVERFETCH_FACTOR,
    SIMILARITY_BLEND,
)
from core_api.schemas import (
    BulkItemResult,
    BulkMemoryCreate,
    BulkMemoryResponse,
    ContradictionInfo,
    EntityLinkOut,
    MemoryCreate,
    MemoryOut,
    MemoryUpdate,
)
from core_api.services.entity_extraction_worker import process_entity_extraction
from core_api.services.entity_tokens import extract_entity_tokens
from core_api.services.hooks import get_hooks
from core_api.services.task_tracker import tracked_task

logger = logging.getLogger(__name__)

# Hardcoded pipeline flags. Intentionally not read from env/Settings: the legacy
# write/search paths are deprecated and scheduled for removal. If an emergency
# rollback is needed, flip these to False and ship a hotfix — do NOT re-introduce
# env-level configuration, since that caused prior silent divergence between
# deployments and the default code path.
_USE_PIPELINE_WRITE = True
_USE_PIPELINE_SEARCH = True


def _content_hash(tenant_id: str, fleet_id: str | None, content: str) -> str:
    return hashlib.sha256(f"{tenant_id}:{fleet_id or ''}:{content}".encode()).hexdigest()


def _auto_chunk_request_id() -> str:
    """Mint a per-row attempt id for in-process bulk-insert callers
    (auto-chunk, atomic-facts) that have no ``X-Bulk-Attempt-Id`` from
    a client (CAURA-602). The ``auto-chunk:`` prefix keeps these rows
    visually distinguishable in the partial unique index from
    real client-side bulk attempts (``f"{X-Bulk-Attempt-Id}:{idx}"``).
    """
    return f"auto-chunk:{uuid4()}"


async def _find_semantic_duplicate(
    db: AsyncSession,
    tenant_id: str,
    fleet_id: str | None,
    embedding: list[float],
    exclude_id: UUID | None = None,
    visibility: str | None = None,
    min_similarity: float | None = None,
) -> dict | None:
    """Find a near-duplicate memory via cosine similarity.

    Returns the closest match above the threshold (or ``None``). The
    returned dict carries a ``similarity`` field — added in A1 #16 so
    two-tier callers can dispatch by score.

    ``min_similarity`` defaults to ``SEMANTIC_DEDUP_THRESHOLD`` (0.95)
    via the storage layer for back-compat. A1 #16's
    ``CheckSemanticDuplicate`` pipeline step passes
    ``SEMANTIC_DEDUP_JUDGE_THRESHOLD`` to surface the judge band.
    """
    sc = get_storage_client()
    payload: dict = {
        "tenant_id": tenant_id,
        "fleet_id": fleet_id,
        "embedding": embedding,
        "exclude_id": str(exclude_id) if exclude_id else None,
        "visibility": visibility,
    }
    if min_similarity is not None:
        payload["min_similarity"] = min_similarity
    return await sc.find_semantic_duplicate(payload)


def _dict_to_memory_out(
    mem: dict,
    entity_links: list[EntityLinkOut] | None = None,
    similarity: float | None = None,
    contradictions: list[ContradictionInfo] | None = None,
) -> MemoryOut:
    """Convert a storage-client dict to MemoryOut."""
    # Explicit None-check: ``{}`` is falsy, so ``or`` would fall
    # through to the legacy ``"metadata"`` key whenever the column
    # is an intentional empty dict, masking the stored value as
    # ``None`` in the API response.
    raw_meta = mem.get("metadata_")
    metadata = raw_meta if raw_meta is not None else mem.get("metadata")
    return MemoryOut(
        id=mem.get("id"),
        tenant_id=mem.get("tenant_id"),
        fleet_id=mem.get("fleet_id"),
        agent_id=mem.get("agent_id"),
        memory_type=mem.get("memory_type"),
        title=mem.get("title"),
        content=mem.get("content"),
        weight=mem.get("weight"),
        source_uri=mem.get("source_uri"),
        run_id=mem.get("run_id"),
        metadata=metadata,
        created_at=mem.get("created_at"),
        expires_at=mem.get("expires_at"),
        entity_links=entity_links or [],
        similarity=similarity,
        subject_entity_id=mem.get("subject_entity_id"),
        predicate=mem.get("predicate"),
        object_value=mem.get("object_value"),
        ts_valid_start=mem.get("ts_valid_start"),
        ts_valid_end=mem.get("ts_valid_end"),
        status=mem.get("status"),
        visibility=mem.get("visibility"),
        recall_count=mem.get("recall_count"),
        last_recalled_at=mem.get("last_recalled_at"),
        supersedes_id=mem.get("supersedes_id"),
        superseded_by=contradictions if contradictions else None,
    )


def _mem_attr(memory, key: str, default=None):
    """Get a field from either an ORM Memory object or a dict."""
    if isinstance(memory, dict):
        return memory.get(key, default)
    return getattr(memory, key, default)


def _memory_to_out(
    memory,
    entity_links: list[EntityLinkOut] | None = None,
    similarity: float | None = None,
    contradictions: list[ContradictionInfo] | None = None,
) -> MemoryOut:
    # See ``_dict_to_memory_out`` for the falsy-``{}`` trap.
    if isinstance(memory, dict):
        raw_meta = memory.get("metadata_")
        metadata = raw_meta if raw_meta is not None else memory.get("metadata")
    else:
        metadata = _mem_attr(memory, "metadata_")
    return MemoryOut(
        id=_mem_attr(memory, "id"),
        tenant_id=_mem_attr(memory, "tenant_id"),
        fleet_id=_mem_attr(memory, "fleet_id"),
        agent_id=_mem_attr(memory, "agent_id"),
        memory_type=_mem_attr(memory, "memory_type"),
        title=_mem_attr(memory, "title"),
        content=_mem_attr(memory, "content"),
        weight=_mem_attr(memory, "weight"),
        source_uri=_mem_attr(memory, "source_uri"),
        run_id=_mem_attr(memory, "run_id"),
        metadata=metadata,
        created_at=_mem_attr(memory, "created_at"),
        expires_at=_mem_attr(memory, "expires_at"),
        entity_links=entity_links or [],
        similarity=similarity,
        subject_entity_id=_mem_attr(memory, "subject_entity_id"),
        predicate=_mem_attr(memory, "predicate"),
        object_value=_mem_attr(memory, "object_value"),
        ts_valid_start=_mem_attr(memory, "ts_valid_start"),
        ts_valid_end=_mem_attr(memory, "ts_valid_end"),
        status=_mem_attr(memory, "status"),
        visibility=_mem_attr(memory, "visibility"),
        recall_count=_mem_attr(memory, "recall_count"),
        last_recalled_at=_mem_attr(memory, "last_recalled_at"),
        supersedes_id=_mem_attr(memory, "supersedes_id"),
        superseded_by=contradictions if contradictions else None,
    )


async def create_memory(db: AsyncSession, data: MemoryCreate) -> MemoryOut:
    if _USE_PIPELINE_WRITE:
        return await _create_memory_pipeline(db, data)
    logger.warning("legacy write path invoked; this path is deprecated and scheduled for removal")
    return await _create_memory_legacy(db, data)


async def _create_memory_pipeline(db: AsyncSession, data: MemoryCreate) -> MemoryOut:
    """Pipeline-based create_memory — same logic, decomposed into timed steps."""
    from core_api.pipeline.compositions.write import (
        build_enrichment_pipeline,
        build_fast_write_pipeline,
        build_stm_write_pipeline,
        build_strong_write_pipeline,
    )
    from core_api.pipeline.context import PipelineContext
    from core_api.services.organization_settings import resolve_config

    # STM branch: bypass tenant config resolution and LTM pipelines entirely
    if data.write_mode == "stm":
        from core_api.config import settings as _stm_settings

        if not _stm_settings.use_stm:
            from fastapi import HTTPException

            raise HTTPException(
                status_code=422,
                detail="STM is not enabled. Set USE_STM=true to enable short-term memory.",
            )
        ctx = PipelineContext(
            db=db,
            data={"input": data, "t0": time.perf_counter()},
        )
        pipeline = build_stm_write_pipeline()
        await pipeline.run(ctx)
        return ctx.data["stm_response"]

    # Resolve tenant config BEFORE building the pipeline
    tenant_config = await resolve_config(db, data.tenant_id)

    # Extract-only and auto-chunk branches: always use the original enrichment+persist flow
    if not data.persist or (
        len(data.content) > CHUNKING_THRESHOLD_CHARS and tenant_config.auto_chunk_enabled
    ):
        ctx = PipelineContext(db=db, data={"input": data, "t0": time.perf_counter()})

        # Phase 1: Enrichment (always runs)
        enrichment_pipeline = build_enrichment_pipeline()
        await enrichment_pipeline.run(ctx)

        fields = ctx.data["memory_fields"]

        # Branch: extract-only
        if not data.persist:
            return MemoryOut(
                id=uuid.uuid4(),
                tenant_id=data.tenant_id,
                fleet_id=data.fleet_id,
                agent_id=data.agent_id,
                memory_type=fields["memory_type"],
                title=fields["title"],
                content=data.content,
                weight=fields["weight"],
                source_uri=data.source_uri,
                run_id=data.run_id,
                # ``fields["metadata"]`` is always a dict — initialised
                # as ``data.metadata or {}`` in MergeEnrichmentFields. The
                # previous ``or None`` coerced an intentional ``{}`` to
                # ``None``, the same falsy-``{}`` trap fixed in the four
                # read-path serializers above. Pass the dict through.
                metadata=fields["metadata"],
                created_at=datetime.now(UTC),
                expires_at=data.expires_at,
                entity_links=[],
                subject_entity_id=data.subject_entity_id,
                predicate=data.predicate,
                object_value=data.object_value,
                ts_valid_start=fields["ts_valid_start"],
                ts_valid_end=fields["ts_valid_end"],
                status=fields["status"],
            )

        # Branch: auto-chunk
        return await _handle_auto_chunk_from_ctx(db, data, ctx)

    # Standard persist path: resolve write mode and pick pipeline
    resolved_mode = _resolve_write_mode(data, tenant_config)

    ctx = PipelineContext(
        db=db,
        data={
            "input": data,
            "t0": time.perf_counter(),
            "resolved_write_mode": resolved_mode,
        },
        tenant_config=tenant_config,
    )

    if resolved_mode == "fast":
        pipeline = build_fast_write_pipeline()
    else:
        pipeline = build_strong_write_pipeline()

    # CAURA-682 Phase 1: per-phase write-latency emission. One line per
    # write request, structured so GCP log queries can slice by tenant /
    # phase to identify the dominant phase under noisy-neighbor load
    # (loadtest finding ``noisy-neighbor-write``, 3.58x degradation).
    # ``phase_timings`` keys are present only for phases that actually
    # ran; missing key = deferred to core-worker via background topic.
    #
    # The emit lives in ``finally`` so timeouts — the actual
    # noisy-neighbor failure mode (``HTTPException(504)`` from
    # ``parallel_embed_enrich`` on ``asyncio.wait_for`` timeout) — also
    # produce a log line with the partial timings that DID land. Without
    # this, the worst case for diagnosis (timed-out writes) is exactly
    # the case that produces no diagnostic signal. ``success`` lets GCP
    # queries filter failed from successful writes.
    _exc: BaseException | None = None
    try:
        try:
            await pipeline.run(ctx)
        except BaseException as e:
            _exc = e
            raise

        memory = ctx.data["memory"]
        return _memory_to_out(
            memory,
            entity_links=[
                EntityLinkOut(entity_id=link.entity_id, role=link.role) for link in data.entity_links
            ],
        )
    finally:
        timings = ctx.data.get("phase_timings", {})
        # Defensive ``.get`` — when the pipeline raised, ``ctx.data["memory"]``
        # may not be set. Explicit ``is not None`` on ``metadata_`` so an
        # empty dict (no flags set yet) isn't treated as falsy and
        # fallthrough'd into ``metadata``.
        memory = ctx.data.get("memory") or {}
        memory_metadata = memory.get("metadata_")
        if memory_metadata is None:
            memory_metadata = memory.get("metadata") or {}
        logger.info(
            "memory_write_latency",
            extra={
                "path": "memory-write",
                "tenant_id": data.tenant_id,
                "agent_id": data.agent_id,
                "fleet_id": data.fleet_id,
                "write_mode": resolved_mode,
                "embedding_ms": timings.get("embedding_ms"),
                "enrichment_ms": timings.get("enrichment_ms"),
                "storage_ms": timings.get("storage_ms"),
                "entity_links_ms": timings.get("entity_links_ms"),
                "total_ms": round((time.perf_counter() - ctx.data["t0"]) * 1000),
                "embedding_pending": bool(memory_metadata.get("embedding_pending")),
                "enrichment_pending": bool(memory_metadata.get("enrichment_pending")),
                "cached_embedding": ctx.data.get("cached_embedding") is not None,
                "success": _exc is None,
            },
        )


async def _handle_auto_chunk_from_ctx(db: AsyncSession, data: MemoryCreate, ctx: object) -> MemoryOut:
    """Auto-chunking branch using pipeline context enrichment results."""
    from core_api.services.ingest_service import _chunk_content

    sc = get_storage_client()
    fields = ctx.data["memory_fields"]
    embedding = ctx.data["embedding"]
    t0 = ctx.data["t0"]
    tenant_config = ctx.tenant_config

    try:
        facts = await _chunk_content(data.content, None, tenant_config)
    except (
        ValueError,
        RuntimeError,
        json.JSONDecodeError,
        OpenAIError,
        GoogleAPIError,
    ):
        logger.exception("Auto-chunking failed; falling through to single-memory path")
        facts = []

    if len(facts) > 1:
        ch = _content_hash(data.tenant_id, data.fleet_id, data.content)
        parent_metadata = dict(fields["metadata"])
        parent_metadata["auto_chunked"] = True
        parent_metadata["child_count"] = len(facts)
        parent_metadata["write_latency_ms"] = round((time.perf_counter() - t0) * 1000)

        # Auto-chunk parent insert — wrapped in the storage bulkhead
        # like the regular single-write path. Auto-chunk fires two
        # storage roundtrips per request (parent here, children below);
        # both count toward the per-tenant ``storage_write`` cap so a
        # tenant doing heavy auto-chunking can't park more storage
        # connections than the cap allows.
        async with per_tenant_storage_slot("storage_write", data.tenant_id):
            parent = await sc.create_memory(
                {
                    "tenant_id": data.tenant_id,
                    "fleet_id": data.fleet_id,
                    "agent_id": data.agent_id,
                    "memory_type": fields["memory_type"],
                    "title": fields["title"],
                    "content": data.content,
                    "embedding": embedding,
                    "weight": fields["weight"],
                    "source_uri": data.source_uri,
                    "run_id": data.run_id,
                    # See ``write_memory_row`` for the falsy-``{}`` trap.
                    "metadata_": parent_metadata,
                    "content_hash": ch,
                    "expires_at": data.expires_at.isoformat() if data.expires_at else None,
                    "subject_entity_id": data.subject_entity_id,
                    "predicate": data.predicate,
                    "object_value": data.object_value,
                    "ts_valid_start": fields["ts_valid_start"].isoformat()
                    if fields.get("ts_valid_start")
                    else None,
                    "ts_valid_end": fields["ts_valid_end"].isoformat()
                    if fields.get("ts_valid_end")
                    else None,
                    "status": fields["status"],
                    "visibility": data.visibility or "scope_team",
                }
            )

        parent_id = parent.get("id")

        _hooks = get_hooks()
        if _hooks.audit_log:
            try:
                await _hooks.audit_log(
                    db,
                    tenant_id=data.tenant_id,
                    agent_id=data.agent_id,
                    action="create",
                    resource_type="memory",
                    resource_id=parent_id,
                    detail={
                        "memory_type": fields["memory_type"],
                        "title": fields["title"],
                        "content_length": len(data.content),
                        "auto_chunked": True,
                        "child_count": len(facts),
                    },
                )
            except Exception:
                logger.warning("Audit hook failed (non-critical)", exc_info=True)

        # Batch embeddings — single API call instead of N sequential calls
        child_texts = [fact["content"] for fact in facts]
        child_embeddings = await get_embeddings_batch(child_texts, tenant_config)

        child_payloads = []
        for fact, child_embedding in zip(facts, child_embeddings):
            child_ch = _content_hash(data.tenant_id, data.fleet_id, fact["content"])
            child_payloads.append(
                {
                    "tenant_id": data.tenant_id,
                    "fleet_id": data.fleet_id,
                    "agent_id": data.agent_id,
                    "memory_type": fact.get("suggested_type", "fact"),
                    "content": fact["content"],
                    "embedding": child_embedding,
                    "weight": fields["weight"],
                    "source_uri": data.source_uri,
                    "run_id": data.run_id,
                    "metadata_": {
                        "parent_memory_id": str(parent_id),
                        "source": "auto_chunk",
                    },
                    "content_hash": child_ch,
                    "client_request_id": _auto_chunk_request_id(),
                    "expires_at": data.expires_at.isoformat() if data.expires_at else None,
                    "status": fields["status"],
                    "visibility": data.visibility or "scope_team",
                }
            )
        # Auto-chunk children — second storage roundtrip in this
        # request after the parent insert above. Same per-tenant cap
        # applies; held only across the bulk call itself.
        async with per_tenant_storage_slot("storage_write", data.tenant_id):
            await sc.create_memories(child_payloads)

        if tenant_config.entity_extraction_enabled:
            track_task(
                tracked_task(
                    process_entity_extraction(
                        parent_id,
                        data.tenant_id,
                        data.fleet_id,
                        data.agent_id,
                        data.content,
                        data.memory_type,
                    ),
                    "entity_extraction",
                    parent_id,
                    data.tenant_id,
                )
            )

        return _dict_to_memory_out(parent)

    # Chunking produced 0-1 facts: fall through to persist pipeline
    from core_api.pipeline.compositions.write import build_persist_pipeline

    persist_pipeline = build_persist_pipeline()
    await persist_pipeline.run(ctx)

    memory = ctx.data["memory"]
    return _memory_to_out(
        memory,
        entity_links=[EntityLinkOut(entity_id=link.entity_id, role=link.role) for link in data.entity_links],
    )


async def _create_memory_legacy(db: AsyncSession, data: MemoryCreate) -> MemoryOut:
    # -- Content quality gate -- reject before any LLM work --
    if len(data.content.strip()) < CRYSTALLIZER_SHORT_CONTENT_CHARS:
        raise HTTPException(
            status_code=422,
            detail=f"Memory content too short (minimum {CRYSTALLIZER_SHORT_CONTENT_CHARS} characters).",
        )

    sc = get_storage_client()
    t0 = time.perf_counter()
    # -- Resolve per-tenant LLM config (falls back to global) --
    from core_api.services.organization_settings import resolve_config

    tenant_config = await resolve_config(db, data.tenant_id)

    # -- Compute content hash early for embedding dedup --
    ch = _content_hash(data.tenant_id, data.fleet_id, data.content) if data.persist else None

    # -- Check for existing embedding from duplicate content (saves LLM call) --
    cached_embedding = None
    if ch:
        cached_embedding = await sc.find_embedding_by_content_hash(
            data.tenant_id,
            ch,
        )

    # -- Enrichment + embedding (needed for both persist and extract-only) --
    enrichment = None
    if cached_embedding is not None:
        logger.info("Reusing existing embedding for content_hash=%s", ch[:12])

        async def _return_cached():
            return cached_embedding

        embedding_task = _return_cached()
    else:
        embedding_task = get_embedding(data.content, tenant_config)

    enrichment_task = None
    if tenant_config.enrichment_enabled and tenant_config.enrichment_provider != "none":
        from core_api.services.memory_enrichment import enrich_memory

        enrichment_task = enrich_memory(data.content, tenant_config)

    if enrichment_task:
        try:
            embedding, enrichment = await asyncio.wait_for(
                asyncio.gather(embedding_task, enrichment_task),
                timeout=20.0,
            )
        except TimeoutError:
            raise HTTPException(status_code=504, detail="Memory enrichment timed out")
    else:
        embedding = await embedding_task

    # Memory enrichment: LLM infers type, weight, title, summary, tags
    memory_type = data.memory_type
    weight = data.weight
    title = None
    metadata = data.metadata or {}
    ts_valid_start = data.ts_valid_start
    ts_valid_end = data.ts_valid_end

    if enrichment:
        # LLM fills gaps; agent-provided values always win
        if memory_type is None:
            memory_type = enrichment.memory_type
        if weight is None:
            weight = enrichment.weight
        title = enrichment.title or None
        if enrichment.summary:
            metadata["summary"] = enrichment.summary
        if enrichment.tags:
            metadata["tags"] = enrichment.tags
        if enrichment.llm_ms:
            metadata["llm_ms"] = enrichment.llm_ms
        # Temporal resolution: LLM-extracted dates fill gaps
        if ts_valid_start is None and enrichment.ts_valid_start:
            ts_valid_start = datetime.fromisoformat(enrichment.ts_valid_start.replace("Z", "+00:00"))
        if ts_valid_end is None and enrichment.ts_valid_end:
            ts_valid_end = datetime.fromisoformat(enrichment.ts_valid_end.replace("Z", "+00:00"))
        # PII detection
        if enrichment.contains_pii:
            metadata["contains_pii"] = True
            if enrichment.pii_types:
                metadata["pii_types"] = enrichment.pii_types

    # Apply defaults if still unset (LLM disabled or failed)
    if memory_type is None:
        memory_type = "fact"
    if weight is None:
        weight = DEFAULT_MEMORY_WEIGHT

    # Status: agent-provided wins, then LLM, then default "active"
    status = data.status
    if not status and enrichment:
        status = getattr(enrichment, "status", None)
    if not status:
        status = "active"

    # -- Extract-only mode: return preview without DB write --
    if not data.persist:
        return MemoryOut(
            id=uuid.uuid4(),
            tenant_id=data.tenant_id,
            fleet_id=data.fleet_id,
            agent_id=data.agent_id,
            memory_type=memory_type,
            title=title,
            content=data.content,
            weight=weight,
            source_uri=data.source_uri,
            run_id=data.run_id,
            # See ``_create_memory_pipeline`` for the falsy-``{}`` trap.
            metadata=metadata,
            created_at=datetime.now(UTC),
            expires_at=data.expires_at,
            entity_links=[],
            subject_entity_id=data.subject_entity_id,
            predicate=data.predicate,
            object_value=data.object_value,
            ts_valid_start=ts_valid_start,
            ts_valid_end=ts_valid_end,
            status=status,
        )

    # -- Auto-chunking: split large direct writes into parent + child memories --
    if data.persist and len(data.content) > CHUNKING_THRESHOLD_CHARS and tenant_config.auto_chunk_enabled:
        from core_api.services.ingest_service import _chunk_content

        try:
            facts = await _chunk_content(data.content, None, tenant_config)
        except (
            ValueError,
            RuntimeError,
            json.JSONDecodeError,
            OpenAIError,
            GoogleAPIError,
        ):
            logger.exception("Auto-chunking failed; falling through to single-memory path")
            facts = []

        if len(facts) > 1:
            ch = _content_hash(data.tenant_id, data.fleet_id, data.content)
            parent_metadata = dict(metadata)
            parent_metadata["auto_chunked"] = True
            parent_metadata["child_count"] = len(facts)
            parent_metadata["write_latency_ms"] = round((time.perf_counter() - t0) * 1000)

            # Legacy-path auto-chunk parent insert. Mirrors the
            # pipeline-path coverage in ``_handle_auto_chunk_from_ctx``.
            async with per_tenant_storage_slot("storage_write", data.tenant_id):
                parent = await sc.create_memory(
                    {
                        "tenant_id": data.tenant_id,
                        "fleet_id": data.fleet_id,
                        "agent_id": data.agent_id,
                        "memory_type": memory_type,
                        "title": title,
                        "content": data.content,
                        "embedding": embedding,
                        "weight": weight,
                        "source_uri": data.source_uri,
                        "run_id": data.run_id,
                        # See ``write_memory_row`` for the falsy-``{}`` trap.
                        "metadata_": parent_metadata,
                        "content_hash": ch,
                        "expires_at": data.expires_at.isoformat() if data.expires_at else None,
                        "subject_entity_id": data.subject_entity_id,
                        "predicate": data.predicate,
                        "object_value": data.object_value,
                        "ts_valid_start": ts_valid_start.isoformat() if ts_valid_start else None,
                        "ts_valid_end": ts_valid_end.isoformat() if ts_valid_end else None,
                        "status": status,
                        "visibility": data.visibility or "scope_team",
                    }
                )

            parent_id = parent.get("id")

            _hooks = get_hooks()
            if _hooks.audit_log:
                try:
                    await _hooks.audit_log(
                        db,
                        tenant_id=data.tenant_id,
                        agent_id=data.agent_id,
                        action="create",
                        resource_type="memory",
                        resource_id=parent_id,
                        detail={
                            "memory_type": memory_type,
                            "title": title,
                            "content_length": len(data.content),
                            "auto_chunked": True,
                            "child_count": len(facts),
                        },
                    )
                except Exception:
                    logger.warning("Audit hook failed (non-critical)", exc_info=True)

            # Batch embeddings — single API call instead of N sequential calls
            child_texts = [fact["content"] for fact in facts]
            child_embeddings = await get_embeddings_batch(child_texts, tenant_config)

            child_payloads = []
            for fact, child_embedding in zip(facts, child_embeddings):
                child_ch = _content_hash(data.tenant_id, data.fleet_id, fact["content"])
                child_payloads.append(
                    {
                        "tenant_id": data.tenant_id,
                        "fleet_id": data.fleet_id,
                        "agent_id": data.agent_id,
                        "memory_type": fact.get("suggested_type", "fact"),
                        "content": fact["content"],
                        "embedding": child_embedding,
                        "weight": weight,
                        "source_uri": data.source_uri,
                        "run_id": data.run_id,
                        "metadata_": {
                            "parent_memory_id": str(parent_id),
                            "source": "auto_chunk",
                        },
                        "content_hash": child_ch,
                        "client_request_id": _auto_chunk_request_id(),
                        "expires_at": data.expires_at.isoformat() if data.expires_at else None,
                        "status": status,
                        "visibility": data.visibility or "scope_team",
                    }
                )
            # Legacy-path auto-chunk children — same bulkhead key as
            # the parent insert above. See ``_handle_auto_chunk_from_ctx``
            # for the pipeline-path equivalent.
            async with per_tenant_storage_slot("storage_write", data.tenant_id):
                await sc.create_memories(child_payloads)

            if tenant_config.entity_extraction_enabled:
                track_task(
                    tracked_task(
                        process_entity_extraction(
                            parent_id,
                            data.tenant_id,
                            data.fleet_id,
                            data.agent_id,
                            data.content,
                            data.memory_type,
                        ),
                        "entity_extraction",
                        parent_id,
                        data.tenant_id,
                    )
                )

            return _dict_to_memory_out(parent)

    # -- Persist path --
    # Dedup: check for exact content match within tenant+fleet, scoped to
    # the writing agent. Cross-agent writes of identical content should
    # succeed as distinct observations — friction §2.8 / Stage 5.
    dup = await sc.find_by_content_hash(
        data.tenant_id,
        ch,
        fleet_id=data.fleet_id,
        agent_id=data.agent_id,
    )
    if dup:
        raise HTTPException(
            status_code=409,
            detail=f"Duplicate memory exists: {dup.get('id')}",
        )

    # Semantic dedup: catch near-duplicates (same meaning, different phrasing)
    if tenant_config.semantic_dedup_enabled and embedding is not None:
        t_dedup = time.perf_counter()
        sem_dup = await _find_semantic_duplicate(
            db,
            data.tenant_id,
            data.fleet_id,
            embedding,
            visibility=data.visibility or "scope_team",
        )
        dedup_ms = round((time.perf_counter() - t_dedup) * 1000, 1)
        metadata["semantic_dedup_ms"] = dedup_ms
        if sem_dup:
            raise HTTPException(
                status_code=409,
                detail=f"Near-duplicate memory exists: {sem_dup.get('id')}",
            )

    if embedding is None:
        metadata["embedding_pending"] = True
        logger.warning("Storing memory without embedding; deferred backfill scheduled")

    # Pre-storage processing latency (embed + enrichment + dedup);
    # excludes the storage-slot queue wait below. We capture it here
    # because the value gets stored ON the row's ``metadata`` column,
    # which must be set before the INSERT — moving the measurement
    # past the storage call would require a follow-up PATCH for a
    # debug-level metric, which isn't worth the extra roundtrip.
    # Operators reading this metric should treat it as
    # "core-api pre-storage time," not "total core-api wall time."
    write_ms = round((time.perf_counter() - t0) * 1000)
    metadata["write_latency_ms"] = write_ms

    # Create memory via storage client
    entity_link_dicts = [{"entity_id": str(link.entity_id), "role": link.role} for link in data.entity_links]

    # CAURA-602 follow-up: per-tenant bulkhead at the storage roundtrip
    # itself. Bounds how many of one tenant's writes can hold storage-
    # writer connections at the same time so a hot tenant can't park
    # the whole pool while a cold tenant's single write queues. The
    # route-entry slot (``per_tenant_slot("write", ...)``) was already
    # held; this slot is held only across the storage call.
    async with per_tenant_storage_slot("storage_write", data.tenant_id):
        created = await sc.create_memory(
            {
                "tenant_id": data.tenant_id,
                "fleet_id": data.fleet_id,
                "agent_id": data.agent_id,
                "memory_type": memory_type,
                "title": title,
                "content": data.content,
                "embedding": embedding,
                "weight": weight,
                "source_uri": data.source_uri,
                "run_id": data.run_id,
                # See ``write_memory_row`` for the falsy-``{}`` trap.
                "metadata_": metadata,
                "content_hash": ch,
                "expires_at": data.expires_at.isoformat() if data.expires_at else None,
                "subject_entity_id": data.subject_entity_id,
                "predicate": data.predicate,
                "object_value": data.object_value,
                "ts_valid_start": ts_valid_start.isoformat() if ts_valid_start else None,
                "ts_valid_end": ts_valid_end.isoformat() if ts_valid_end else None,
                "status": status,
                "visibility": data.visibility or "scope_team",
                "entity_links": entity_link_dicts,
            }
        )

    # Total core-api wall time (embed + enrich + dedup + storage-slot
    # queue + storage roundtrip). The row-level ``write_latency_ms`` in
    # ``metadata_`` covers the pre-storage portion only; under
    # storage-slot contention (CAURA-602 follow-up) those two values
    # diverge, and operators investigating a tenant-storm latency spike
    # need the wall-time figure to localise the source. Renaming the
    # row metric would break operator dashboards built against
    # historical data, so we leave it intact and emit total time as a
    # structured log line — DEBUG-level so steady-state load doesn't
    # drown the signal but the value is queryable when needed.
    total_ms = round((time.perf_counter() - t0) * 1000)
    logger.debug(
        "single-write latency",
        extra={
            "tenant_id": data.tenant_id,
            "agent_id": data.agent_id,
            "prestorage_ms": write_ms,
            "total_ms": total_ms,
            "storage_slot_wait_ms": total_ms - write_ms,
        },
    )

    memory_id = created.get("id")

    detail = {
        "memory_type": memory_type,
        "title": title,
        "content_length": len(data.content),
        "write_latency_ms": write_ms,
    }

    _hooks = get_hooks()
    if _hooks.audit_log:
        try:
            await _hooks.audit_log(
                db,
                tenant_id=data.tenant_id,
                agent_id=data.agent_id,
                action="create",
                resource_type="memory",
                resource_id=memory_id,
                detail=detail,
            )
        except Exception:
            logger.warning("Audit hook failed (non-critical)", exc_info=True)

    # Post-commit async tasks (fire-and-forget)
    if tenant_config.entity_extraction_enabled:
        track_task(
            tracked_task(
                process_entity_extraction(
                    memory_id,
                    data.tenant_id,
                    data.fleet_id,
                    data.agent_id,
                    data.content,
                    data.memory_type,
                ),
                "entity_extraction",
                memory_id,
                data.tenant_id,
            )
        )

    # CAURA-594: under ``deployment_mode=deferred`` this is the
    # deferred path (parallel_embed_enrich.py skipped the provider
    # call by design); under ``inline``, embedding is None means an
    # inline failure to retry. The shim picks the right backend.
    if embedding is None:
        track_task(
            tracked_task(
                _schedule_embed_or_reembed(memory_id, data.content, data.tenant_id, content_hash=ch),
                "embed_or_publish",
                memory_id,
                data.tenant_id,
            )
        )
    else:
        # P1-1: Contradiction detection moved to post-commit async
        from core_api.services.contradiction_detector import detect_contradictions_async

        track_task(
            tracked_task(
                detect_contradictions_async(
                    memory_id,
                    data.tenant_id,
                    data.fleet_id,
                    data.content,
                    embedding,
                ),
                "contradiction_detection",
                memory_id,
                data.tenant_id,
            )
        )

    return _dict_to_memory_out(
        created,
        entity_links=[EntityLinkOut(entity_id=link.entity_id, role=link.role) for link in data.entity_links],
    )


async def create_memories_bulk(
    db: AsyncSession,
    data: BulkMemoryCreate,
    *,
    bulk_attempt_id: str,
) -> BulkMemoryResponse:
    """Create multiple memories with per-attempt idempotency (CAURA-602).

    The route binds each item to a stable ``client_request_id`` of the
    form ``f"{bulk_attempt_id}:{index}"``. Storage's per-item unique
    constraint then turns retries into deterministic outcomes:

    - first attempt → ``status="created"`` per item.
    - retry of the same ``X-Bulk-Attempt-Id`` after a lost response →
      every previously-committed row resolves to ``duplicate_attempt``
      with the canonical id, so no row is ever silently committed.
    - same content already exists from an earlier *different* attempt →
      ``duplicate_content``, matching today's content-hash dedup
      semantics.
    - validation, embed/enrich budget, or storage-side missing id →
      ``error`` per item.

    Embed + enrich + content-hash pre-dedup runs as before; the storage
    call returns one entry per surviving item, and we map by
    ``client_request_id`` so input order is preserved without relying on
    Postgres ``RETURNING`` order.
    """
    sc = get_storage_client()
    t0 = time.perf_counter()
    items = data.items
    n = len(items)

    # -- Per-item validation. Short content used to raise a 422 for the
    # whole batch; now it's a per-item "error" result. Indices in this
    # dict are skipped through the embed / enrich / dedup / write path.
    short_content_errors: dict[int, str] = {
        i: f"content too short (minimum {CRYSTALLIZER_SHORT_CONTENT_CHARS} characters)"
        for i, item in enumerate(items)
        if len(item.content.strip()) < CRYSTALLIZER_SHORT_CONTENT_CHARS
    }

    # -- Resolve per-tenant config once --
    from core_api.services.organization_settings import resolve_config

    tenant_config = await resolve_config(db, data.tenant_id)

    # -- Batch embeddings + parallel enrichment (valid items only). Short
    # items are skipped so we don't spend provider budget on content
    # that will surface as an error anyway.
    valid_indices = [i for i in range(n) if i not in short_content_errors]
    embeddings: list = [None] * n
    if valid_indices and settings.inline_embedding:
        try:
            async with asyncio.timeout(BULK_EMBEDDING_TIMEOUT_SECONDS):
                valid_embeddings = await get_embeddings_batch(
                    [items[i].content for i in valid_indices], tenant_config
                )
        except TimeoutError:
            raise HTTPException(status_code=504, detail="Bulk embedding timed out")
        for emb_pos, item_idx in enumerate(valid_indices):
            embeddings[item_idx] = valid_embeddings[emb_pos]

    enrichments: list = [None] * n
    # CAURA-595: ``deployment_mode=deferred`` defers the LLM call to
    # ``core-worker``; the bulk-persist loop below still proceeds with
    # all-None enrichments and the publish happens post-persist (one
    # event per successfully-created row).
    if (
        valid_indices
        and settings.inline_enrichment
        and tenant_config.enrichment_enabled
        and tenant_config.enrichment_provider != "none"
    ):
        from core_api.services.memory_enrichment import enrich_memory

        sem = asyncio.Semaphore(BULK_ENRICHMENT_CONCURRENCY)

        async def _enrich(idx: int):
            async with sem:
                try:
                    enrichments[idx] = await enrich_memory(
                        items[idx].content,
                        tenant_config,
                        reference_datetime=items[idx].reference_datetime,
                    )
                except (ValueError, RuntimeError, OpenAIError, GoogleAPIError):
                    logger.warning("Enrichment failed for bulk item %d", idx)

        try:
            async with asyncio.timeout(BULK_ENRICHMENT_TOTAL_TIMEOUT_SECONDS):
                await asyncio.gather(*[_enrich(i) for i in valid_indices])
        except TimeoutError:
            logger.warning(
                "Bulk enrichment exceeded %ss budget; proceeding with partial results",
                BULK_ENRICHMENT_TOTAL_TIMEOUT_SECONDS,
            )

    # -- Batch hash dedup: compute all hashes, query storage API in one shot.
    # Storage returns ``{content_hash: {id, client_request_id}}`` so the
    # per-item classifier below can split content matches into
    # ``duplicate_attempt`` (this caller's own prior commit) vs
    # ``duplicate_content`` (a different attempt's row).
    hashes = [_content_hash(data.tenant_id, data.fleet_id, item.content) for item in items]

    existing_hashes: dict[str, dict] = {}
    if hashes:
        # Stage 5: scope bulk dedup to (tenant, fleet, agent) so a batch
        # from agent-A and a batch from agent-B in the same fleet don't
        # collide on identical content.
        existing_hashes = await sc.bulk_find_by_content_hashes(
            data.tenant_id,
            hashes,
            fleet_id=data.fleet_id,
            agent_id=data.agent_id,
        )

    # -- Also detect intra-batch duplicates (same content appearing twice) --
    seen_hashes: dict[str, int] = {}  # hash -> first index

    # -- Build memories and track results --
    results: list[BulkItemResult | None] = [None] * n
    # Each queued entry pairs the original input index with the row dict
    # we'll send to storage. Carrying ``orig_idx`` alongside the dict
    # avoids a parallel ``memory_index_map`` list, and the dict isn't
    # mutated with the index because storage's column-filter would drop
    # an unknown key on the way in.
    pending: list[tuple[int, dict]] = []
    created_count = 0
    dup_count = 0
    error_count = 0

    for i, item in enumerate(items):
        # Server-derived per-item attempt id. Stable across retries
        # because ``bulk_attempt_id`` is the client-supplied
        # ``X-Bulk-Attempt-Id`` and the index is positional within the
        # request body — same body + same attempt id ⇒ same per-item id.
        item_request_id = f"{bulk_attempt_id}:{i}"

        # Short-content items surface as per-item errors; never embedded,
        # enriched, deduped, or written.
        if i in short_content_errors:
            results[i] = BulkItemResult(
                index=i,
                client_request_id=item_request_id,
                status="error",
                error=short_content_errors[i],
            )
            error_count += 1
            continue

        ch = hashes[i]

        # An existing row matches this content. Two flavours:
        #   - ``duplicate_attempt``: the stored row's
        #     ``client_request_id`` equals the per-item id we're about
        #     to claim — i.e. *this* caller's prior commit landed and
        #     we're seeing our own retry. ``duplicate_of`` is omitted
        #     because the row IS this attempt's row, not a foreign one.
        #   - ``duplicate_content``: a different attempt (or a legacy
        #     row with NULL ``client_request_id``) wrote the same
        #     content first. Same semantics as the pre-CAURA-602
        #     ``"duplicate"`` state.
        if ch in existing_hashes:
            existing = existing_hashes[ch]
            existing_id = existing["id"]
            # Subscript (not ``.get()``) so a future router shape
            # regression that drops the key surfaces as KeyError instead
            # of silently misclassifying every retry as
            # ``duplicate_content``. The router is the contract owner;
            # see ``bulk_find_by_content_hashes`` in
            # core-storage-api/routers/memories.py.
            if existing["client_request_id"] == item_request_id:
                results[i] = BulkItemResult(
                    index=i,
                    client_request_id=item_request_id,
                    status="duplicate_attempt",
                    id=existing_id,
                )
            else:
                results[i] = BulkItemResult(
                    index=i,
                    client_request_id=item_request_id,
                    status="duplicate_content",
                    id=existing_id,
                    duplicate_of=existing_id,
                )
            dup_count += 1
            continue

        # Intra-batch duplicate: two items with identical content in
        # the same call. Surface as ``duplicate_content`` for caller
        # consistency with the cross-batch case — both states mean
        # "this row was not the canonical writer of the content."
        if ch in seen_hashes:
            results[i] = BulkItemResult(
                index=i,
                client_request_id=item_request_id,
                status="duplicate_content",
            )
            dup_count += 1
            continue
        seen_hashes[ch] = i

        # Apply enrichment
        enrichment = enrichments[i]
        memory_type = item.memory_type
        weight = item.weight
        title = None
        metadata = item.metadata or {}
        ts_valid_start = item.ts_valid_start
        ts_valid_end = item.ts_valid_end

        if enrichment:
            if memory_type is None:
                memory_type = enrichment.memory_type
            if weight is None:
                weight = enrichment.weight
            title = enrichment.title or None
            if enrichment.summary:
                metadata["summary"] = enrichment.summary
            if enrichment.tags:
                metadata["tags"] = enrichment.tags
            if enrichment.llm_ms:
                metadata["llm_ms"] = enrichment.llm_ms
            if ts_valid_start is None and enrichment.ts_valid_start:
                ts_valid_start = datetime.fromisoformat(enrichment.ts_valid_start.replace("Z", "+00:00"))
            if ts_valid_end is None and enrichment.ts_valid_end:
                ts_valid_end = datetime.fromisoformat(enrichment.ts_valid_end.replace("Z", "+00:00"))
            if enrichment.contains_pii:
                metadata["contains_pii"] = True
                if enrichment.pii_types:
                    metadata["pii_types"] = enrichment.pii_types

        if memory_type is None:
            memory_type = "fact"
        if weight is None:
            weight = DEFAULT_MEMORY_WEIGHT

        status = item.status
        if not status and enrichment:
            status = getattr(enrichment, "status", None)
        if not status:
            status = "active"

        entity_link_dicts = [
            {"entity_id": str(link.entity_id), "role": link.role} for link in item.entity_links
        ]

        mem_data = {
            "tenant_id": data.tenant_id,
            "fleet_id": data.fleet_id,
            "agent_id": data.agent_id,
            "memory_type": memory_type,
            "title": title,
            "content": item.content,
            "embedding": embeddings[i],
            "weight": weight,
            "source_uri": item.source_uri,
            "run_id": item.run_id,
            # See ``write_memory_row`` for the falsy-``{}`` trap. The
            # bulk path doesn't append ``write_latency_ms`` so an item
            # with ``item.metadata={}`` and no enrichment-added keys
            # genuinely reaches here as ``{}`` — pass it through so the
            # column stores ``{}`` instead of NULL.
            "metadata_": metadata,
            "content_hash": ch,
            "client_request_id": item_request_id,
            "expires_at": item.expires_at.isoformat() if item.expires_at else None,
            "subject_entity_id": item.subject_entity_id,
            "predicate": item.predicate,
            "object_value": item.object_value,
            "ts_valid_start": ts_valid_start.isoformat() if ts_valid_start else None,
            "ts_valid_end": ts_valid_end.isoformat() if ts_valid_end else None,
            "status": status,
            "visibility": data.visibility or "scope_team",
            "entity_links": entity_link_dicts,
        }
        pending.append((i, mem_data))

    # -- Bulk insert via storage client. The storage layer returns one
    # entry per submitted item with ``was_inserted`` distinguishing
    # newly-committed rows from those resolved against a prior attempt's
    # commit (the silent-create eliminator). The legacy ReadTimeout
    # reconcile branch is intentionally gone — its job is now done at
    # the row level by the per-attempt unique constraint, and a retry of
    # the entire request is the documented recovery path.
    #
    # Per-tenant storage bulkhead (CAURA-602 follow-up): the slot wraps
    # only the storage roundtrip itself, holding tight while the call
    # is in flight and releasing as soon as the storage response (or
    # cancellation) returns. Embed/enrich already finished above and
    # the audit/contradiction/reembed fan-out below runs as
    # fire-and-forget tasks, so the slot's grip on storage stays tight
    # even for long-tail batches.
    if pending:
        # Per-phase deadline on the storage roundtrip itself (CAURA-599).
        # Without this the only deadline on this phase is the outer
        # ``bulk_request_timeout_seconds`` umbrella in the route handler;
        # a hung storage call would consume any unused embed/enrich slack
        # before the 504 path fires.
        #
        # Order matters: ``asyncio.timeout`` is the OUTER context manager
        # so the deadline arms before ``per_tenant_storage_slot`` calls
        # ``asyncio.Semaphore.acquire()`` (which blocks indefinitely under
        # contention). Compound ``async with (A, B):`` enters left-to-right,
        # so swapping the order would leave the slot wait outside the
        # deadline. Cancellation during a queued acquire raises cleanly
        # from inside the semaphore's __aenter__ — no slot is held, so
        # no release is needed on the timeout path.
        async with (
            asyncio.timeout(settings.storage_bulk_timeout_seconds),
            per_tenant_storage_slot("storage_write", data.tenant_id),
        ):
            storage_results = await sc.create_memories([d for _, d in pending])

        # Map each storage result back to its source item via
        # ``client_request_id``. Postgres ``RETURNING`` order is
        # unspecified for ``ON CONFLICT DO NOTHING``, so we never
        # zip on positional index.
        by_request_id = {r["client_request_id"]: r for r in storage_results}

        # Track the (orig_idx, mem_data, mem_id) trios for the
        # successfully-resolved items so the audit log + background
        # task loops below operate on rows that actually exist.
        resolved: list[tuple[int, dict, str]] = []

        for orig_idx, mem_data in pending:
            crid = mem_data["client_request_id"]
            sr = by_request_id.get(crid)
            if sr is None or not sr.get("id"):
                # Storage didn't return a row for this id — concurrent
                # soft-delete or schema drift. Surface as per-item
                # error rather than fabricating an id.
                results[orig_idx] = BulkItemResult(
                    index=orig_idx,
                    client_request_id=crid,
                    status="error",
                    error="storage did not return an id for this item",
                )
                error_count += 1
                continue

            mem_id = sr["id"]
            if sr.get("was_inserted"):
                results[orig_idx] = BulkItemResult(
                    index=orig_idx,
                    client_request_id=crid,
                    status="created",
                    id=mem_id,
                )
                created_count += 1
                resolved.append((orig_idx, mem_data, mem_id))
            else:
                # Same attempt id already committed — i.e. a retry. The
                # row exists; we don't re-run audit / entity-extraction
                # / contradiction tasks for it because the original
                # attempt already kicked those off (or will, if its
                # tracked tasks haven't drained yet).
                results[orig_idx] = BulkItemResult(
                    index=orig_idx,
                    client_request_id=crid,
                    status="duplicate_attempt",
                    id=mem_id,
                )
                dup_count += 1

        # Back-fill ``id`` / ``duplicate_of`` on intra-batch
        # ``duplicate_content`` rows. The first-occurrence loop above
        # marks them with no canonical id — at the time we couldn't
        # know it, since the canonical row hadn't been written yet.
        # Now that ``results[seen_hashes[ch]]`` carries the storage id
        # (whether it's ``created`` or ``duplicate_attempt``), copy it
        # forward so the ``BulkItemResult`` docstring contract holds:
        # ``duplicate_content`` always has both fields populated.
        for j in range(n):
            later = results[j]
            if later is None or later.status != "duplicate_content" or later.id is not None:
                continue
            canonical = results[seen_hashes[hashes[j]]]
            if canonical is None or canonical.id is None:
                # Canonical row never persisted (storage error) — leaving
                # this slot as a contract-violating
                # ``duplicate_content`` with both id fields ``None``
                # would silently break clients that branch on status.
                # Downgrade to ``error`` and rebalance the aggregate
                # counts: we'd previously incremented ``dup_count`` for
                # this item, undo that.
                results[j] = BulkItemResult(
                    index=j,
                    client_request_id=later.client_request_id,
                    status="error",
                    error="canonical row for intra-batch duplicate did not persist",
                )
                dup_count -= 1
                error_count += 1
                continue
            results[j] = BulkItemResult(
                index=j,
                client_request_id=later.client_request_id,
                status="duplicate_content",
                id=canonical.id,
                duplicate_of=canonical.id,
            )

        # Bulk audit log — only for newly-inserted rows. Fire-and-forget
        # so a parent ``asyncio.wait_for`` cancellation (e.g. the 90s
        # bulk budget firing after storage commits but before the
        # in-flight audit call returns) can't strand committed rows
        # without their audit records: the retry sees those rows as
        # ``duplicate_attempt`` and never re-enters this block, so the
        # audit had to land on the original attempt or it's lost. The
        # tasks reference ``data.tenant_id`` etc. by value, not the
        # request-scoped ``db`` session (``log_action`` doesn't actually
        # touch ``db`` — it calls ``sc.create_audit_log`` over HTTP),
        # so a teardown of the request context after cancellation
        # doesn't affect them.
        _hooks = get_hooks()
        if _hooks.audit_log:
            for _orig_idx, mem_data, mem_id in resolved:
                track_task(
                    tracked_task(
                        _hooks.audit_log(
                            db,
                            tenant_id=data.tenant_id,
                            agent_id=data.agent_id,
                            action="create",
                            resource_type="memory",
                            resource_id=mem_id,
                            detail={
                                "memory_type": mem_data["memory_type"],
                                "title": mem_data.get("title"),
                                "content_length": len(mem_data["content"]),
                                "source": "bulk",
                            },
                        ),
                        "audit_log",
                        mem_id,
                        data.tenant_id,
                    )
                )

        # Fire-and-forget async tasks for each newly-created memory.
        # ``duplicate_attempt`` rows skip these — the original attempt
        # already enqueued them (and re-running would double-bill the
        # LLM provider for entity extraction + enrichment).
        from core_api.services.contradiction_detector import detect_contradictions_async

        # CAURA-595: per-row enrich publishes when deployment_mode is deferred.
        defer_enrich_publish = (
            not settings.inline_enrichment
            and tenant_config.enrichment_enabled
            and tenant_config.enrichment_provider != "none"
        )

        reembed_batch: list[tuple[UUID, str]] = []
        for orig_idx, mem_data, mem_id in resolved:
            if tenant_config.entity_extraction_enabled:
                track_task(
                    tracked_task(
                        process_entity_extraction(
                            mem_id,
                            data.tenant_id,
                            data.fleet_id,
                            data.agent_id,
                            items[orig_idx].content,
                            mem_data["memory_type"],
                        ),
                        "entity_extraction",
                        mem_id,
                        data.tenant_id,
                    )
                )
            if defer_enrich_publish:
                # ``defer_enrich_publish`` already encodes
                # ``not settings.inline_enrichment`` — call the
                # publisher directly instead of routing through
                # ``_schedule_enrich_or_inline`` whose mode-check would
                # be dead code at this site.
                track_task(
                    tracked_task(
                        publish_memory_enrich_request(
                            memory_id=mem_id,
                            content=items[orig_idx].content,
                            tenant_id=data.tenant_id,
                            tenant_config=tenant_config,
                            reference_datetime=items[orig_idx].reference_datetime,
                            agent_provided_fields=_agent_provided_enrichment_fields(items[orig_idx]),
                        ),
                        "enrich_publish",
                        mem_id,
                        data.tenant_id,
                    )
                )
            if embeddings[orig_idx] is None:
                reembed_batch.append((mem_id, items[orig_idx].content))
            else:
                track_task(
                    tracked_task(
                        detect_contradictions_async(
                            mem_id,
                            data.tenant_id,
                            data.fleet_id,
                            items[orig_idx].content,
                            embeddings[orig_idx],
                        ),
                        "contradiction_detection",
                        mem_id,
                        data.tenant_id,
                    )
                )
        if reembed_batch:
            # memory_id is None: no single UUID is authoritative for a
            # batch. _reembed_memories_bulk logs per-item failures with
            # the correct ID; the wrapper-level failure row (if any)
            # just captures the batch-level exception.
            track_task(
                tracked_task(
                    _reembed_memories_bulk(reembed_batch, data.tenant_id),
                    f"reembed_bulk[{len(reembed_batch)}]",
                    None,
                    data.tenant_id,
                )
            )

    # Every slot in ``results`` is filled by now — short-content errors,
    # content/intra-batch duplicates, or post-storage outcomes. Surface
    # any gap loudly: ``-O`` strips bare ``assert`` and a silent filter
    # would hide an entire row from the response, which is exactly the
    # silent-create class this PR is meant to close.
    unfilled = [i for i, r in enumerate(results) if r is None]
    if unfilled:
        logger.error("bulk results contain unfilled slots at indices %s", unfilled)
        raise HTTPException(
            status_code=500,
            detail="internal error: unfilled bulk result slots",
        )
    final_results = cast("list[BulkItemResult]", results)

    bulk_ms = round((time.perf_counter() - t0) * 1000)
    return BulkMemoryResponse(
        created=created_count,
        duplicates=dup_count,
        errors=error_count,
        results=final_results,
        bulk_ms=bulk_ms,
    )


_REEMBED_MAX_RETRIES = 3
_REEMBED_BACKOFF_BASE_S = 10


async def _schedule_embed_or_reembed(
    memory_id: UUID,
    content: str,
    tenant_id: str,
    *,
    content_hash: str | None = None,
) -> None:
    """Backfill the embedding for a memory persisted with ``embedding=NULL``.

    Inline mode (OSS, no worker fleet): in-process retry via
    :func:`_reembed_memory`.
    Deferred mode (SaaS): publish ``EMBED_REQUESTED``; ``core-worker``
    PATCHes the row. ``content_hash`` is used by the worker to short-
    circuit the provider call when the same content was already
    embedded for this tenant — pass it whenever the caller has it in
    scope.
    """
    if settings.inline_embedding:
        await _reembed_memory(memory_id, content, tenant_id)
    else:
        await publish_memory_embed_request(
            memory_id=memory_id,
            content=content,
            tenant_id=tenant_id,
            content_hash=content_hash,
        )


# Columns the agent may set explicitly on ``MemoryCreate`` /
# ``BulkMemoryItem`` that the enricher would otherwise overwrite. Used
# below to compute ``agent_provided_fields`` from
# ``Pydantic.model_fields_set`` for the worker's PATCH gate.
_ENRICHMENT_AGENT_OVERRIDE_FIELDS: frozenset[str] = frozenset(
    {
        "memory_type",
        "weight",
        "status",
        "ts_valid_start",
        "ts_valid_end",
    }
)


def _assert_override_fields_match_schemas() -> None:
    """Catch typos / schema drift at import time.

    The override-skip gate is the only thing protecting agent-provided
    values from being silently downgraded by the worker on every
    redelivery (``EnrichmentResult``'s Pydantic defaults survive
    ``model_dump(exclude_none=True)``). A misspelled name in the
    frozenset above would be invisible — the gate would simply never
    match, and data corruption would only surface as user complaints
    days later.
    """
    from core_api.schemas import BulkMemoryItem, MemoryCreate

    for cls in (MemoryCreate, BulkMemoryItem):
        missing = _ENRICHMENT_AGENT_OVERRIDE_FIELDS - set(cls.model_fields)
        if missing:
            raise RuntimeError(
                f"_ENRICHMENT_AGENT_OVERRIDE_FIELDS references fields "
                f"missing from {cls.__name__}: {sorted(missing)}"
            )


_assert_override_fields_match_schemas()


def _agent_provided_enrichment_fields(
    data: object,
) -> list[str] | None:
    """Snapshot which enrichment columns the agent set explicitly.

    Reads ``Pydantic.BaseModel.model_fields_set`` if available — it
    contains exactly the fields the request body had a value for, before
    Pydantic applied defaults. The worker uses the result as the
    ``agent_provided_fields`` PATCH-skip list so a redelivery (or a
    slow worker run) can't downgrade an agent-provided ``weight=0.9``
    back to the schema default.

    Returns ``None`` (= "trust enrichment for everything") when
    ``data`` doesn't expose ``model_fields_set`` — keeps the helper
    safe against synthetic test inputs.
    """
    fields_set = getattr(data, "model_fields_set", None)
    if not fields_set:
        return None
    overlap = sorted(_ENRICHMENT_AGENT_OVERRIDE_FIELDS & fields_set)
    return overlap or None


async def _schedule_enrich_or_inline(
    memory_id: UUID,
    content: str,
    tenant_id: str,
    fleet_id: str | None,
    agent_id: str,
    tenant_config: object,
    *,
    agent_provided_fields: list[str] | None = None,
    reference_datetime: datetime | None = None,
) -> None:
    """Enrichment counterpart of :func:`_schedule_embed_or_reembed`.

    Inline mode (OSS / pre-CAURA-595 default): run enrichment as an
    in-process background task in core-api via
    :func:`_enrich_memory_background`.
    Deferred mode (CAURA-595 SaaS): publish ``ENRICH_REQUESTED``;
    ``core-worker`` consumes the event, runs the LLM, and PATCHes the
    enrichment fields back. The worker also emits ``ENRICHED`` after
    the PATCH lands.

    ``agent_provided_fields`` is forwarded so the worker doesn't
    overwrite anything the agent set explicitly at write time —
    critical because ``EnrichmentResult``'s Pydantic defaults survive
    ``model_dump(exclude_none=True)`` and would otherwise downgrade
    those columns on every redelivery.
    """
    if settings.inline_enrichment:
        # NOTE: ``agent_provided_fields`` and ``reference_datetime``
        # are intentionally NOT forwarded to ``_enrich_memory_background``.
        # The inline path pre-dates these concepts and uses an
        # equivalent gate by reading the row's current value vs the
        # schema default (``if mem.get("memory_type") == "fact" and
        # enrichment.memory_type:`` etc., see the body of
        # ``_enrich_memory_background``). When the agent set
        # ``memory_type="rule"`` the column already reads ``"rule"``
        # not ``"fact"``, and the inline gate skips.
        #
        # The two paths converge on the same agent-wins outcome via
        # different mechanisms; unifying them on
        # ``agent_provided_fields`` is a reasonable cleanup but out of
        # scope for CAURA-595 — there's no observable behavioural
        # delta between the gates today.
        await _enrich_memory_background(memory_id, content, tenant_id, fleet_id, agent_id)
    else:
        await publish_memory_enrich_request(
            memory_id=memory_id,
            content=content,
            tenant_id=tenant_id,
            tenant_config=tenant_config,
            reference_datetime=reference_datetime,
            agent_provided_fields=agent_provided_fields,
        )


async def _reembed_memory(
    memory_id: UUID,
    content: str,
    tenant_id: str,
    *,
    is_failure_fallback: bool = False,
) -> None:
    """Background task: (optionally wait,) retry embedding, patch the row,
    then run contradiction detection.

    The initial sleep is skipped by default — the common caller is the
    deliberate hot-path offload, where waiting 30s would blow the
    sub-2s freshness SLA. Callers that ARE retrying a just-failed
    provider call (e.g. batched re-embed falling back to per-item)
    must pass ``is_failure_fallback=True`` to get the backoff, otherwise
    N serial retries land on the already-failing provider with zero
    delay — thundering herd.
    """
    from core_api.constants import EMBEDDING_REEMBED_DELAY_S
    from core_api.services.organization_settings import resolve_config

    if settings.inline_embedding or is_failure_fallback:
        # Two paths land here: pre-offload legacy behaviour (inline mode,
        # this coroutine only runs on provider failure) and CAURA-594
        # batch-fallback (deferred mode but the batch just failed). Both
        # want the backoff.
        await asyncio.sleep(EMBEDDING_REEMBED_DELAY_S)
    try:
        tenant_config = await resolve_config(None, tenant_id)
    except Exception:
        logger.warning("Failed to resolve tenant config for re-embed (tenant=%s)", tenant_id, exc_info=True)
        tenant_config = None

    embedding = None
    for attempt in range(1, _REEMBED_MAX_RETRIES + 1):
        embedding = await get_embedding(content, tenant_config=tenant_config)
        if embedding is not None:
            break
        delay = _REEMBED_BACKOFF_BASE_S * attempt
        logger.warning(
            "Re-embed attempt %d/%d failed for memory %s, retrying in %ds",
            attempt,
            _REEMBED_MAX_RETRIES,
            memory_id,
            delay,
        )
        await asyncio.sleep(delay)
    if embedding is None:
        logger.error(
            "Background re-embed exhausted all %d retries for memory %s",
            _REEMBED_MAX_RETRIES,
            memory_id,
        )
        return

    try:
        sc = get_storage_client()
        mem = await sc.get_memory(str(memory_id))
        if mem is None or mem.get("deleted_at") is not None:
            return
        # Race guard: _enrich_memory_background may have already written a
        # hint-enhanced embedding for this row. Respect it (higher retrieval
        # quality than our raw-content embedding) and just fire contradiction
        # detection on the existing value instead of overwriting.
        #
        # Mode-agnostic: the race exists even under ``deployment_mode=
        # "inline"`` — in fast write mode with enrichment enabled, a
        # hot-path embed failure causes ScheduleBackgroundTasks to
        # queue BOTH _reembed and _enrich_memory_background, so enrich
        # can beat us to the row regardless of the deploy mode.
        if mem.get("embedding") is not None:
            from core_api.services.contradiction_detector import detect_contradictions_async

            track_task(
                tracked_task(
                    detect_contradictions_async(
                        memory_id,
                        tenant_id,
                        mem.get("fleet_id"),
                        content,
                        mem.get("embedding"),
                    ),
                    "contradiction_detection_post_reembed",
                    memory_id,
                    tenant_id,
                )
            )
            return
        await sc.update_embedding(str(memory_id), embedding)
        logger.info("Background re-embed succeeded for memory %s", memory_id)
    except (TimeoutError, ValueError, RuntimeError, OpenAIError, GoogleAPIError):
        logger.exception("Background re-embed error for memory %s", memory_id)
        return

    # Contradiction coverage: the write path only fires contradiction
    # detection when an embedding is present at write-time. Deferred items
    # would silently skip it unless we fire it here.
    from core_api.services.contradiction_detector import detect_contradictions_async

    track_task(
        tracked_task(
            detect_contradictions_async(
                memory_id,
                tenant_id,
                mem.get("fleet_id"),
                content,
                embedding,
            ),
            "contradiction_detection_post_reembed",
            memory_id,
            tenant_id,
        )
    )


async def _reembed_memories_bulk(
    items: list[tuple[UUID, str]],
    tenant_id: str,
) -> None:
    """Batched background re-embed for bulk-originated memories.

    One ``get_embeddings_batch`` call covers all items, preserving the
    packing behaviour the hot-path bulk code used. Any per-item failure
    falls back to ``_reembed_memory`` so a partial batch doesn't leave
    some rows without embeddings forever.
    """
    from core_api.services.contradiction_detector import detect_contradictions_async
    from core_api.services.organization_settings import resolve_config

    if not items:
        return
    try:
        tenant_config = await resolve_config(None, tenant_id)
    except Exception:
        # Config miss: continue with None so get_embeddings_batch can
        # use its default provider rather than stranding the whole batch.
        logger.warning(
            "Failed to resolve tenant config for bulk re-embed (tenant=%s)", tenant_id, exc_info=True
        )
        tenant_config = None

    try:
        # 30s cap matches the hot-path bulk embed in create_memories_bulk;
        # an unbounded provider call in a background task would pin a
        # Cloud Run worker thread if Vertex / OpenAI hangs.
        embeddings = await asyncio.wait_for(
            get_embeddings_batch([content for _, content in items], tenant_config),
            timeout=30.0,
        )
    except Exception:
        # Broad on purpose: any provider failure — auth, HTTP client
        # errors, connection-pool exhaustion, Vertex quota — must still
        # land the batch in the per-item fallback, otherwise those
        # exception types strand all N items permanently unembedded.
        # asyncio.CancelledError inherits from BaseException, not
        # Exception, so shutdown-path cancellations still propagate.
        logger.exception("Bulk re-embed batch call failed; falling back to per-item re-embed")
        for memory_id, content in items:
            track_task(
                tracked_task(
                    # is_failure_fallback=True: provider just failed for
                    # the whole batch, so the per-item retries need the
                    # 30s backoff to avoid a thundering herd.
                    _reembed_memory(memory_id, content, tenant_id, is_failure_fallback=True),
                    "reembed",
                    memory_id,
                    tenant_id,
                )
            )
        return

    # Materialise the strict zip up front so a length mismatch surfaces as
    # a single ValueError we can fall back on, instead of raising
    # partway through (leaving some items written and some not).
    try:
        pairs = list(zip(items, embeddings, strict=True))
    except ValueError:
        logger.exception(
            "Bulk re-embed: embedding count mismatch (expected %d); falling back to per-item re-embed",
            len(items),
        )
        for memory_id, content in items:
            track_task(
                tracked_task(
                    _reembed_memory(memory_id, content, tenant_id, is_failure_fallback=True),
                    "reembed",
                    memory_id,
                    tenant_id,
                )
            )
        return

    sc = get_storage_client()

    # Fan out the get_memory reads concurrently — O(N) serial awaits
    # was a real cliff for large bulks (a 100-item batch with 50ms
    # storage p99 = 5s wall-clock before the first PATCH). gather with
    # return_exceptions=True so one failed read doesn't nuke the rest.
    mems = await asyncio.gather(
        *[sc.get_memory(str(memory_id)) for (memory_id, _), _ in pairs],
        return_exceptions=True,
    )

    for ((memory_id, content), embedding), mem in zip(pairs, mems):
        if embedding is None:
            track_task(
                tracked_task(
                    # Per-item None after a partial batch success is
                    # still a provider partial-failure — back off.
                    _reembed_memory(memory_id, content, tenant_id, is_failure_fallback=True),
                    "reembed",
                    memory_id,
                    tenant_id,
                )
            )
            continue
        if isinstance(mem, BaseException):
            # A transient get_memory failure here would otherwise strand
            # this item permanently unembedded — the batch helper is the
            # only scheduled writer. Reschedule as a per-item retry.
            logger.error(
                "Bulk re-embed: get_memory failed for %s; scheduling per-item retry",
                memory_id,
                exc_info=mem,
            )
            track_task(
                tracked_task(
                    _reembed_memory(memory_id, content, tenant_id, is_failure_fallback=True),
                    "reembed",
                    memory_id,
                    tenant_id,
                )
            )
            continue
        if mem is None or mem.get("deleted_at") is not None:
            continue
        # Mirror the single-item race guard in _reembed_memory: if
        # _enrich_memory_background has already written a hint-enhanced
        # embedding, respect it (higher retrieval quality) and fire
        # contradiction detection on the existing value instead.
        if mem.get("embedding") is not None:
            track_task(
                tracked_task(
                    detect_contradictions_async(
                        memory_id,
                        tenant_id,
                        mem.get("fleet_id"),
                        content,
                        mem.get("embedding"),
                    ),
                    "contradiction_detection_post_reembed",
                    memory_id,
                    tenant_id,
                )
            )
            continue
        try:
            await sc.update_embedding(str(memory_id), embedding)
        except Exception:
            # Broad match for the same reason as the outer batch-call
            # except: httpx-layer errors, pool exhaustion, auth, etc.
            # aren't in the narrow tuple and would otherwise propagate
            # out of the for-loop, aborting the rest of the batch.
            # CancelledError (BaseException subclass) still propagates.
            # Reschedule the item so a transient PATCH blip doesn't
            # leave it permanently unembedded.
            logger.exception(
                "Bulk re-embed PATCH failed for memory %s; scheduling per-item retry",
                memory_id,
            )
            track_task(
                tracked_task(
                    _reembed_memory(memory_id, content, tenant_id, is_failure_fallback=True),
                    "reembed",
                    memory_id,
                    tenant_id,
                )
            )
            continue
        track_task(
            tracked_task(
                detect_contradictions_async(
                    memory_id,
                    tenant_id,
                    mem.get("fleet_id"),
                    content,
                    embedding,
                ),
                "contradiction_detection_post_reembed",
                memory_id,
                tenant_id,
            )
        )


_STRONG_TYPES = frozenset({"decision", "commitment", "cancellation"})


def _resolve_write_mode(data: MemoryCreate, tenant_config) -> str:
    """Pure function: resolve the effective write mode from caller hint + tenant config."""
    mode = data.write_mode
    if mode in ("fast", "strong", "stm"):
        return mode
    # Auto: high-stakes types -> strong
    if data.memory_type in _STRONG_TYPES:
        return "strong"
    # Tenant default (falls back to "fast")
    return tenant_config.default_write_mode


async def _enrich_memory_background(
    memory_id: UUID,
    content: str,
    tenant_id: str,
    fleet_id: str | None,
    agent_id: str,
) -> None:
    """Background task: run LLM enrichment on a fast-path memory, then patch the row.

    After enrichment completes, fires entity extraction and contradiction detection
    as sub-tasks.
    """
    from core_api.services.memory_enrichment import enrich_memory
    from core_api.services.organization_settings import resolve_config
    from core_api.services.task_tracker import tracked_task

    try:
        tenant_config = await resolve_config(None, tenant_id)
    except Exception:
        logger.exception("Background enrichment: failed to resolve config for memory %s", memory_id)
        return

    if not tenant_config.enrichment_enabled:
        return

    try:
        enrichment = await enrich_memory(content, tenant_config)
    except (ValueError, RuntimeError, OpenAIError, GoogleAPIError):
        logger.exception("Background enrichment LLM call failed for memory %s", memory_id)
        return

    if enrichment is None:
        return

    try:
        sc = get_storage_client()
        mem = await sc.get_memory(str(memory_id))
        if mem is None or mem.get("deleted_at") is not None:
            return

        # Build update patch
        patch: dict = {}
        if mem.get("memory_type") == "fact" and enrichment.memory_type:
            patch["memory_type"] = enrichment.memory_type
        if mem.get("weight") == 0.5 and enrichment.weight is not None:
            patch["weight"] = enrichment.weight
        if enrichment.title:
            patch["title"] = enrichment.title

        # See ``_dict_to_memory_out`` for the falsy-``{}`` trap.
        raw_meta = mem.get("metadata_")
        existing = raw_meta if raw_meta is not None else mem.get("metadata")
        meta = dict(existing) if existing is not None else {}
        if enrichment.summary:
            meta["summary"] = enrichment.summary
        if enrichment.tags:
            meta["tags"] = enrichment.tags
        if enrichment.llm_ms:
            meta["llm_ms"] = enrichment.llm_ms
        if enrichment.contains_pii:
            meta["contains_pii"] = True
            if enrichment.pii_types:
                meta["pii_types"] = enrichment.pii_types
        if enrichment.retrieval_hint:
            # Persisted for debugging / auditability only; no longer used
            # to shape the embedding (see CAURA-222).
            meta["retrieval_hint"] = enrichment.retrieval_hint
        # Temporal resolution
        if mem.get("ts_valid_start") is None and enrichment.ts_valid_start:
            patch["ts_valid_start"] = enrichment.ts_valid_start
        if mem.get("ts_valid_end") is None and enrichment.ts_valid_end:
            patch["ts_valid_end"] = enrichment.ts_valid_end
        # Status: only upgrade from default "active"
        if mem.get("status") == "active" and enrichment.status:
            patch["status"] = enrichment.status

        meta.pop("enrichment_pending", None)
        patch["metadata_"] = meta

        # Apply patch via storage client -- use update_memory_status for status
        # and a general patch for other fields
        if patch:
            # The storage API update_memory_status handles status changes;
            # for other fields we need to build the right call
            status_val = patch.pop("status", None)
            if patch:
                # Use a generic memory patch (metadata, type, weight, etc.)
                # Fall back to update via scored-search patch endpoint
                await sc._patch(f"/memories/{memory_id}", patch)
            if status_val:
                await sc.update_memory_status(str(memory_id), status_val)

        memory_type = patch.get("memory_type") or mem.get("memory_type")

        # Hint-based re-embed removed (CAURA-222): the hot path embeds raw
        # ``content`` and the search side embeds raw query, so the stored
        # vector is already on the correct surface by the time enrichment
        # finishes. Re-embedding ``content`` here would just produce an
        # identical vector — wasted provider call and DB write. F3 Phase 3
        # also removed the SaaS-mode contradiction-coverage branch that
        # used to live here; deferred-mode contradiction detection is now
        # fully owned by the worker consumers (handle_memory_embedded /
        # handle_memory_enriched), and OSS-mode contradiction fires
        # through the inline write pipeline before this function runs.

        # Atomic-fact fan-out: if the enricher detected 2+ independent claims in
        # this turn, create a child memory for each so queries targeting a
        # specific fact retrieve it directly. Children embed raw
        # ``fact_content`` (not hint-prefixed) to keep the same write/query
        # surface as the search side — see CAURA-222. Failures here are
        # non-fatal to the parent.
        atomic_facts = getattr(enrichment, "atomic_facts", None) or []
        if len(atomic_facts) >= 1:
            parent_ts_start = mem.get("ts_valid_start")
            parent_visibility = mem.get("visibility") or "scope_team"
            parent_weight = patch.get("weight") or mem.get("weight") or 0.5
            fanout_created = 0
            for fact in atomic_facts:
                fact_content = fact.content
                try:
                    child_embedding = await get_embedding(fact_content, tenant_config=tenant_config)
                except (TimeoutError, ValueError, RuntimeError, OpenAIError, GoogleAPIError):
                    logger.warning(
                        "atomic-fact embed failed for memory %s (skipping this fact)",
                        memory_id,
                        exc_info=True,
                    )
                    continue
                if child_embedding is None:
                    continue
                child_ch = _content_hash(tenant_id, fleet_id, fact_content)
                child_meta = {
                    "parent_memory_id": str(memory_id),
                    "source": "atomic_fact_fanout",
                    "retrieval_hint": fact.retrieval_hint or "",
                }
                # Intentionally NOT wrapped in ``per_tenant_storage_slot``
                # (CAURA-602 follow-up): this site runs inside
                # ``_enrich_memory_background``, a fire-and-forget task
                # with no outer request budget. The bulkhead's
                # unbounded-queue contract relies on an outer deadline
                # to cap wait time; without one, a saturated tenant
                # could pile fan-out tasks behind hot-path requests
                # indefinitely. The fan-out is rare enough (only fires
                # when the LLM extracts >1 atomic fact from a parent)
                # that letting it bypass the cap is the safer trade —
                # but if loadtest data ever shows it materially driving
                # storage-pool occupancy, revisit by giving the task
                # its own deadline first.
                try:
                    await sc.create_memory(
                        {
                            "tenant_id": tenant_id,
                            "fleet_id": fleet_id,
                            "agent_id": agent_id,
                            "memory_type": fact.suggested_type,
                            "content": fact_content,
                            "embedding": child_embedding,
                            "weight": parent_weight,
                            "metadata_": child_meta,
                            "content_hash": child_ch,
                            "status": "active",
                            "visibility": parent_visibility,
                            "ts_valid_start": parent_ts_start,
                        }
                    )
                    fanout_created += 1
                except (TimeoutError, ValueError, RuntimeError, OpenAIError, GoogleAPIError):
                    logger.warning(
                        "atomic-fact create_memory failed for parent %s",
                        memory_id,
                        exc_info=True,
                    )
            if fanout_created:
                logger.info(
                    "atomic-fact fan-out created %d children for parent %s",
                    fanout_created,
                    memory_id,
                )

        # Fire sub-tasks outside the session
        if tenant_config.entity_extraction_enabled:
            track_task(
                tracked_task(
                    process_entity_extraction(
                        memory_id,
                        tenant_id,
                        fleet_id,
                        agent_id,
                        content,
                        memory_type,
                    ),
                    "entity_extraction",
                    memory_id,
                    tenant_id,
                )
            )
        # F3 Phase 3 removed the asymmetric ``(embed=deferred,
        # enrich=inline)`` race-guard branch that previously lived here.
        # Under ``deployment_mode`` the two axes co-vary, so the branch
        # was unreachable in any real deployment: inline mode entered
        # this function with ``inline_embedding=True`` (guard short-
        # circuits); deferred mode never enters this function because
        # ``_schedule_enrich_or_inline`` publishes ``ENRICH_REQUESTED``
        # instead. Contradiction detection on the deferred path is now
        # owned solely by the ``handle_memory_embedded`` and
        # ``handle_memory_enriched`` consumers in ``consumer.py`` —
        # they fire ``detect_contradictions_async`` when their
        # respective worker PATCHes land.
        logger.info("Background enrichment succeeded for memory %s", memory_id)
    except (TimeoutError, ValueError, RuntimeError, SQLAlchemyError, OpenAIError, GoogleAPIError):
        logger.exception("Background enrichment error for memory %s", memory_id)


async def soft_delete_memory(db: AsyncSession, memory_id: UUID, tenant_id: str) -> None:
    sc = get_storage_client()
    mem = await sc.get_memory_for_tenant(tenant_id, str(memory_id))
    if not mem:
        raise HTTPException(status_code=404, detail="Memory not found")

    await sc.soft_delete_memory(str(memory_id))

    _hooks = get_hooks()
    if _hooks.audit_log:
        try:
            await _hooks.audit_log(
                db,
                tenant_id=tenant_id,
                agent_id=mem.get("agent_id"),
                action="soft_delete",
                resource_type="memory",
                resource_id=memory_id,
            )
        except Exception:
            logger.warning("Audit hook failed (non-critical)", exc_info=True)


async def update_memory(
    db: AsyncSession,
    memory_id: UUID,
    tenant_id: str,
    data: MemoryUpdate,
    agent_id: str | None = None,
) -> MemoryOut:
    """Update a memory. Re-embeds and re-extracts entities if content changes."""
    from core_api.services.organization_settings import resolve_config

    sc = get_storage_client()
    mem = await sc.get_memory_for_tenant(tenant_id, str(memory_id))
    if not mem:
        raise HTTPException(status_code=404, detail="Memory not found")

    # Trust enforcement -- always runs (access control, not a platform feature)
    if agent_id:
        from core_api.services.agent_service import authorize_memory_access, enforce_update

        await enforce_update(db, tenant_id, agent_id, mem.get("agent_id"))
        # Cross-fleet / scope_agent row authorization (write threshold) — the
        # same fleet/scope contract the list/search paths enforce, so a by-id
        # PATCH can't mutate a peer fleet's row.
        allowed = await authorize_memory_access(
            db,
            tenant_id,
            agent_id,
            visibility=mem.get("visibility"),
            owner_agent_id=mem.get("agent_id"),
            fleet_id=mem.get("fleet_id"),
            write=True,
        )
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Agent '{agent_id}' cannot modify memory in fleet '{mem.get('fleet_id')}'.",
            )

    fields_set = data.model_fields_set
    if not fields_set:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Snapshot old values for audit diff
    changes: dict = {}
    content_changed = "content" in fields_set and data.content != mem.get("content")

    new_embedding = None
    # Content change: re-embed, re-hash, check dedup
    if content_changed:
        tenant_config = await resolve_config(db, tenant_id)
        new_embedding = await get_embedding(data.content, tenant_config)
        new_hash = _content_hash(tenant_id, mem.get("fleet_id"), data.content)

        # Dedup check (exclude self)
        dup = await sc.find_duplicate_hash(
            tenant_id,
            new_hash,
            exclude_id=str(memory_id),
        )
        if dup:
            raise HTTPException(status_code=409, detail=f"Duplicate memory exists: {dup.get('id')}")

        # Semantic dedup on content change (exclude self; skip when new embedding is None)
        if tenant_config.semantic_dedup_enabled and new_embedding is not None:
            sem_dup = await _find_semantic_duplicate(
                db,
                tenant_id,
                mem.get("fleet_id"),
                new_embedding,
                exclude_id=memory_id,
            )
            if sem_dup:
                raise HTTPException(
                    status_code=409,
                    detail=f"Near-duplicate memory exists: {sem_dup.get('id')}",
                )

        changes["content"] = {"old": mem.get("content", "")[:200], "new": data.content[:200]}

    # Build patch dict for storage client
    patch: dict = {}
    if content_changed:
        patch["content"] = data.content
        if new_embedding is not None:
            patch["embedding"] = new_embedding
        patch["content_hash"] = _content_hash(tenant_id, mem.get("fleet_id"), data.content)
        # P1-2: Clear stale contradiction/supersession state on content change
        if mem.get("supersedes_id") is not None:
            patch["supersedes_id"] = None
        if mem.get("status") in ("outdated", "conflicted"):
            patch["status"] = "active"

    # Apply simple field updates
    simple_fields = {
        "memory_type": "memory_type",
        "weight": "weight",
        "title": "title",
        "status": "status",
        "visibility": "visibility",
        "source_uri": "source_uri",
        "subject_entity_id": "subject_entity_id",
        "predicate": "predicate",
        "object_value": "object_value",
        "ts_valid_start": "ts_valid_start",
        "ts_valid_end": "ts_valid_end",
        "expires_at": "expires_at",
    }
    for field_name, attr_name in simple_fields.items():
        if field_name in fields_set:
            old_val = mem.get(attr_name)
            new_val = getattr(data, field_name)
            if old_val != new_val:
                changes[field_name] = {
                    "old": str(old_val)[:200] if old_val is not None else None,
                    "new": str(new_val)[:200] if new_val is not None else None,
                }
                # Serialize datetime fields for JSON transport
                val = new_val
                if isinstance(val, datetime):
                    val = val.isoformat()
                patch[attr_name] = val

    # Metadata: merge by default (load-test review feedback —
    # ``patch-metadata-replace`` MEDIUM finding). Pre-2026-04-26 this
    # silently overwrote the column wholesale, so a status-only PATCH
    # would drop unrelated keys (e.g. ``ground_truth``). The new
    # default routes through the storage layer's ``metadata_patch``
    # synthetic key (single top-level JSONB ``||`` merge — note: not
    # recursive; nested dicts are replaced wholesale); explicit
    # ``metadata_mode="replace"`` opts back into the old behaviour.
    #
    # ``metadata_mode`` defaults to ``None`` in the schema; treat
    # ``None`` as ``"merge"`` here so SDK clients that serialise with
    # ``exclude_none=True`` don't have to set it explicitly.
    #
    # ``{"metadata": null}`` in merge mode raises 400 rather than
    # silently no-op'ing: pre-PR that payload cleared the column,
    # and silently changing the contract would be a data-integrity
    # regression for any caller that relied on null-as-clear. Force
    # them to opt into ``replace`` so the intent is explicit.
    if "metadata" in fields_set:
        effective_mode = data.metadata_mode or "merge"
        # Explicit None-check: ``{}`` is falsy, so ``or`` would
        # silently fall through to the legacy ``"metadata"`` key
        # whenever the stored column is an intentional empty dict —
        # corrupting the audit-log ``old`` field.
        raw_meta = mem.get("metadata_")
        old_meta = raw_meta if raw_meta is not None else mem.get("metadata")
        if effective_mode == "replace":
            changes["metadata"] = {
                "old": old_meta,
                "new": data.metadata,
                "mode": "replace",
            }
            patch["metadata_"] = data.metadata
        elif data.metadata is None:
            # Surface the breaking change explicitly: pre-PR
            # ``{"metadata": null}`` cleared the column. The
            # ``Deprecation`` header lets ops/SDK catch lingering
            # callers without crawling 400 logs.
            raise HTTPException(
                status_code=400,
                headers={"Deprecation": "true"},
                detail=(
                    "metadata=null has no effect in merge mode; "
                    'use metadata_mode="replace" to clear the column'
                ),
            )
        elif data.metadata:
            # Top-level JSONB ``||`` merge at the storage layer.
            # Nested objects are replaced, not recursively merged.
            # Same shape the CAURA-595 enrich worker uses.
            changes["metadata"] = {
                "old": old_meta,
                "new": data.metadata,
                "mode": "merge",
            }
            patch["metadata_patch"] = data.metadata
        # else (empty dict in merge mode) → storage no-op, no audit
        # entry, no patch field.

    # Entity links: replace if explicitly provided
    if "entity_links" in fields_set and data.entity_links is not None:
        entity_link_dicts = [
            {"entity_id": str(link.entity_id), "role": link.role} for link in data.entity_links
        ]
        await sc.update_memory_entities(str(memory_id), entity_link_dicts)
        changes["entity_links"] = {
            "old": "replaced",
            "new": f"{len(data.entity_links)} links",
        }

    # Apply the patch via storage client
    if patch:
        await sc._patch(f"/memories/{memory_id}", patch)

    # Audit log — only fire when something actually changed. The
    # ``elif data.metadata`` guard above already prevents falsy
    # merge-mode patches from contributing to ``patch`` / ``changes``;
    # without the corresponding guard here, the hook would still
    # record a phantom "update" event with empty ``changes`` for a
    # ``{"metadata": null}`` request (or a ``metadata_mode``-only
    # request — though the schema validator now rejects that case
    # at the Pydantic boundary).
    _hooks = get_hooks()
    if _hooks.audit_log and (changes or patch):
        try:
            await _hooks.audit_log(
                db,
                tenant_id=tenant_id,
                agent_id=agent_id or mem.get("agent_id"),
                action="update",
                resource_type="memory",
                resource_id=memory_id,
                detail={"changes": changes, "content_changed": content_changed},
            )
        except Exception:
            logger.warning("Audit hook failed (non-critical)", exc_info=True)

    # Re-fetch updated memory
    updated = await sc.get_memory(str(memory_id))

    # Post-commit async tasks for content changes
    if content_changed:
        tenant_config = await resolve_config(db, tenant_id)
        if tenant_config.entity_extraction_enabled:
            track_task(
                tracked_task(
                    process_entity_extraction(
                        memory_id,
                        tenant_id,
                        updated.get("fleet_id"),
                        updated.get("agent_id"),
                        updated.get("content"),
                        updated.get("memory_type"),
                    ),
                    "entity_extraction",
                    memory_id,
                    tenant_id,
                )
            )
        # P1-2: Re-check contradictions after content update
        from core_api.services.contradiction_detector import detect_contradictions_async

        track_task(
            tracked_task(
                detect_contradictions_async(
                    memory_id,
                    tenant_id,
                    updated.get("fleet_id"),
                    updated.get("content"),
                    new_embedding,
                ),
                "contradiction_detection",
                memory_id,
                tenant_id,
            )
        )

    # Load entity links for response
    links_data = await sc.get_entity_links_for_memories([str(memory_id)])
    entity_links = [
        EntityLinkOut(entity_id=el.get("entity_id"), role=el.get("role"))
        for el in links_data.get(str(memory_id), [])
    ]

    return _dict_to_memory_out(updated, entity_links=entity_links)


# Re-exported from EntityRepository for backward compatibility (tests, pipeline steps).
from core_api.repositories.entity_repository import _relation_weight  # noqa: F401


async def expand_graph(
    db: AsyncSession,
    seed_entity_ids: list[UUID],
    tenant_id: str,
    fleet_id: str | None,
    max_hops: int = GRAPH_MAX_HOPS,
    use_union: bool = False,
) -> dict[UUID, tuple[int, float]]:
    """Expand entity graph via storage client."""
    sc = get_storage_client()
    result = await sc.expand_graph(
        {
            "seed_entity_ids": [str(eid) for eid in seed_entity_ids],
            "tenant_id": tenant_id,
            "fleet_id": fleet_id,
            "max_hops": max_hops,
            "use_union": use_union,
        }
    )
    # Convert dict keys back to UUIDs and values to (hop, weight) tuples
    return {
        UUID(k): (
            v.get("hop", 0) if isinstance(v, dict) else v[0],
            v.get("weight", 1.0) if isinstance(v, dict) else v[1],
        )
        for k, v in result.items()
    }


def _is_specific_token(token: str) -> bool:
    """Check if a token looks like a proper noun, identifier, or ticker."""
    if not token:
        return False
    # All-caps acronym (NEXAI, BTC, GPT) or CamelCase/PascalCase (CertiK, OpenAI)
    if token.isupper() or (token[0].isupper() and any(c.isupper() for c in token[1:])):
        return True
    # Contains digits (IDs, versions, codes): "gpt-5", "1892347"
    if any(c.isdigit() for c in token):
        return True
    # Starts with special chars (tickers, handles): "$SOL", "@karpathy", "#trending"
    if token[0] in ("$", "@", "#"):
        return True
    return False


def _adaptive_fts_weight(query: str) -> float:
    """Return a boosted FTS weight for short, specific queries.

    A27 — shares the canonical tokenizer with the entity-FTS gate so a
    hyphenated query like ``claude-opus-4-7`` no longer produces a
    1-token view here but a 4-token view at ``extract_entity_tokens``.
    Two behavioural details are preserved across the share:

    1. ``MAX_TOKENS`` gates on RAW whitespace count, not on the
       post-filter token count. A 4+ word natural-language sentence
       should stay default-weight even when the shared filter would
       collapse it to a single meaningful token (``"tell me about
       NEXAI"`` → semantic-heavy, don't boost).
    2. Sigil tokens (``$BTC`` / ``@karpathy`` / ``#trending``) are
       detected on the RAW query before ``extract_entity_tokens``
       strips leading punctuation, so the handle / ticker / hashtag
       signal that ``_is_specific_token`` keys on still fires after
       the share.
    """
    from core_api.services.entity_tokens import extract_entity_tokens

    raw = query.split()
    if len(raw) > FTS_BOOST_MAX_TOKENS:
        return FTS_WEIGHT

    tokens = extract_entity_tokens(query, preserve_case=True)
    sigil_count = sum(1 for t in raw if t and t[0] in ("$", "@", "#") and len(t) > 1)

    if not tokens and sigil_count == 0:
        return FTS_WEIGHT

    specific_count = sum(1 for t in tokens if _is_specific_token(t)) + sigil_count
    denom = len(tokens) or 1
    if specific_count / denom > FTS_BOOST_SPECIFICITY_RATIO:
        return FTS_WEIGHT_BOOSTED

    return FTS_WEIGHT


def _normalize_query_for_cache(query: str) -> str:
    """Normalize query for cache key: lowercase, strip, collapse whitespace."""
    return re.sub(r"\s+", " ", query.strip().lower())


# Per-process stampede guard for cold-cache embed calls. Maps cache-key to
# the in-flight Future producing the embedding. When N concurrent callers
# miss the cache for the same key, the first arrival registers the Future
# and fires the embed call; subsequent arrivals find the Future and await
# its result instead of each issuing their own OpenAI round-trip.
#
# Scope is intentionally per-process: a Redis-side lock would coordinate
# across replicas but the latency floor of the read path is already a
# single embed call per cold key per replica, and the stampede pattern
# observed in production is dominated by the same client issuing N
# parallel recalls (e.g. fan-out probe, agent batch) hitting the same
# replica. Cache_set still happens, so once any replica completes the
# embed, the next request — local OR remote — finds it in Redis.
_inflight_embeddings: dict[str, asyncio.Future] = {}


async def _get_or_cache_embedding(query: str, tenant_id: str, tenant_config):
    """Get embedding from cache or generate it.

    The cache key includes ``VECTOR_DIM`` so that a schema-dimension
    migration doesn't surface stale cached embeddings to the new schema;
    old entries with a mismatched dim become unreachable under the new
    key and expire naturally via ``EMBEDDING_CACHE_TTL``.

    Concurrent cold-cache callers for the same key share a single
    ``get_query_embedding`` round-trip via ``_inflight_embeddings`` —
    measured 3-second tail spread on 5 parallel novel-query recalls
    pre-fix; post-fix all callers join the leader's future.
    """
    from core_api.cache import cache_get, cache_set

    _model = (
        getattr(tenant_config, "embedding_model", None) if tenant_config is not None else None
    ) or OPENAI_EMBEDDING_MODEL
    _normalized = _normalize_query_for_cache(query)
    _qhash = hashlib.sha256(f"{_model}:{VECTOR_DIM}:{tenant_id}:{_normalized}".encode()).hexdigest()
    # Prefix bumped from ``qemb2:`` → ``qemb3:`` because the hash input
    # changed (added ``VECTOR_DIM``). The bump makes the
    # cache-generation boundary explicit in Redis key stats so an
    # operator can see the cold-start at deploy time and confirm the
    # embedding provider can absorb the working-set re-fetch. Old
    # ``qemb2:*`` entries expire naturally via ``EMBEDDING_CACHE_TTL``.
    _cache_key = f"qemb3:{_qhash}"
    _cached_raw = await cache_get(_cache_key)
    if _cached_raw is not None:
        try:
            return json.loads(_cached_raw)
        except (ValueError, TypeError):
            pass

    # Cold cache: check whether another coroutine is already producing
    # this embedding. If so, await its result. Otherwise become the
    # leader, register the future, and fire the embed call.
    inflight = _inflight_embeddings.get(_cache_key)
    if inflight is not None:
        return await inflight

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _inflight_embeddings[_cache_key] = fut
    try:
        # Search-side embed uses the instruction-aware path. For symmetric
        # models (OpenAI, bge, snowflake-m, gte-en-v1.5) this is identical to
        # ``get_embedding(query, tenant_config)``. For instruction-aware models
        # (Qwen3-Embedding, e5-instruct), the provider prepends the configured
        # task instruction (env: ``EMBEDDING_QUERY_INSTRUCTION``) so the query
        # is encoded with the same instruction prefix the model was trained on.
        # Documents (writes) embed unmodified text.
        embedding = await asyncio.wait_for(get_query_embedding(query, tenant_config), timeout=10.0)
        if embedding is None:
            raise ValueError("Embedding service unavailable")
        await cache_set(_cache_key, json.dumps(embedding), ttl=EMBEDDING_CACHE_TTL)
        fut.set_result(embedding)
        return embedding
    except BaseException as exc:
        # Propagate to every waiter so they raise consistently rather
        # than hanging forever on a fulfilled-never future. BaseException
        # catches CancelledError too — leaking a cancelled future would
        # otherwise strand every joiner. The leader re-raises after the
        # finally block.
        if not fut.done():
            fut.set_exception(exc)
        raise
    finally:
        # Drop the inflight slot only after the future has been resolved.
        # A late arrival that read the slot before this pop still gets
        # the cached future and awaits its already-resolved state.
        _inflight_embeddings.pop(_cache_key, None)


async def _entity_boost_pipeline(
    db: AsyncSession,
    query: str,
    tenant_id: str,
    fleet_ids: list[str] | None,
    graph_expand: bool,
    graph_max_hops: int,
    use_union: bool = False,
    precomputed_hops: dict[UUID, tuple[int, float]] | None = None,
) -> tuple[set[UUID], dict[UUID, float]]:
    """Run entity FTS matching -> graph expansion -> link collection.

    Returns (boosted_memory_ids, memory_boost_factor).
    Independent of embedding -- can run in parallel.

    When *precomputed_hops* is supplied (from ClassifyQuery fallthrough),
    entity FTS and graph expansion are skipped to avoid double DB roundtrips.
    """
    sc = get_storage_client()
    boosted_memory_ids: set[UUID] = set()
    memory_boost_factor: dict[UUID, float] = {}

    try:
        if precomputed_hops is not None:
            entity_hops = precomputed_hops
            matched_entity_ids = [eid for eid, (hop, _w) in entity_hops.items() if hop == 0]
        else:
            tokens = extract_entity_tokens(query)
            if not tokens:
                return boosted_memory_ids, memory_boost_factor

            # Entity FTS search via storage client
            matched_entity_ids_raw = await sc.fts_search_entities(
                {
                    "tenant_id": tenant_id,
                    "tokens": tokens,
                    "fleet_ids": fleet_ids,
                }
            )
            matched_entity_ids = [UUID(eid) for eid in matched_entity_ids_raw]

            if not matched_entity_ids:
                return boosted_memory_ids, memory_boost_factor

            # Graph expansion
            if graph_expand and graph_max_hops > 0:
                entity_hops = await expand_graph(
                    db,
                    matched_entity_ids,
                    tenant_id,
                    fleet_ids[0] if fleet_ids and len(fleet_ids) == 1 else None,
                    max_hops=graph_max_hops,
                    use_union=use_union,
                )
            else:
                entity_hops = dict.fromkeys(matched_entity_ids, (0, 1.0))

        if matched_entity_ids:
            # Collect memories linked to discovered entities via storage client
            all_entity_ids = list(entity_hops.keys())
            all_links_raw = await sc.get_memory_ids_by_entity_ids([str(eid) for eid in all_entity_ids])

            # Process links in hop order (closest entities first)
            all_links = [
                (
                    UUID(item["memory_id"]) if isinstance(item["memory_id"], str) else item["memory_id"],
                    UUID(item["entity_id"]) if isinstance(item["entity_id"], str) else item["entity_id"],
                )
                for item in all_links_raw
            ]
            all_links.sort(key=lambda row: entity_hops.get(row[1], (999, 0.0))[0])

            for mem_id, ent_id in all_links:
                hop, rel_weight = entity_hops.get(ent_id, (0, 1.0))
                hop_boost = GRAPH_HOP_BOOST.get(hop, GRAPH_HOP_BOOST[max(GRAPH_HOP_BOOST)])
                boost = hop_boost * rel_weight
                if mem_id not in memory_boost_factor or boost > memory_boost_factor[mem_id]:
                    memory_boost_factor[mem_id] = boost
                if len(memory_boost_factor) >= GRAPH_MAX_BOOSTED_MEMORIES:
                    break

            # Multi-entity boost
            if len(matched_entity_ids) > 1:
                memory_entity_count: dict[UUID, int] = {}
                matched_set = set(matched_entity_ids)
                for mem_id, ent_id in all_links:
                    if ent_id in matched_set:
                        memory_entity_count[mem_id] = memory_entity_count.get(mem_id, 0) + 1
                for mem_id in memory_boost_factor:
                    count = memory_entity_count.get(mem_id, 0)
                    if count > 1 and memory_boost_factor[mem_id] > 1.0:
                        extra = min(count - 1, 4) * 0.10
                        memory_boost_factor[mem_id] = min(
                            memory_boost_factor[mem_id] * (1.0 + extra),
                            RECALL_BOOST_CAP,
                        )

            boosted_memory_ids = set(memory_boost_factor.keys())
    except (SQLAlchemyError, ValueError):
        logger.exception("Entity/graph boost lookup failed (falling back to pure vector search)")

    return boosted_memory_ids, memory_boost_factor


def _extract_temporal_hint(query: str) -> timedelta | None:
    """Extract a temporal scope from query for freshness boosting."""
    import re

    q = query.lower()

    if re.search(r"\b(today|this morning|tonight)\b", q):
        return timedelta(days=1)
    if re.search(r"\b(yesterday)\b", q):
        return timedelta(days=2)
    if re.search(r"\b(last few days)\b", q):
        return timedelta(days=5)
    if re.search(r"\b(this week|past week)\b", q):
        return timedelta(days=7)
    if re.search(r"\b(last week)\b", q):
        return timedelta(days=14)
    if re.search(r"\b(this month|past month)\b", q):
        return timedelta(days=30)
    if re.search(r"\b(last month)\b", q):
        return timedelta(days=60)
    if re.search(r"\b(this quarter)\b", q):
        return timedelta(days=90)
    if re.search(r"\b(this year)\b", q):
        return timedelta(days=365)
    if re.search(r"\b(last year)\b", q):
        return timedelta(days=730)

    m = re.search(r"\blast\s+(\d+)\s+(day|week|month)s?\b", q)
    if m:
        n = int(m.group(1))
        if n == 0:
            return None
        unit = m.group(2)
        if unit == "day":
            return timedelta(days=min(n, 365))
        if unit == "week":
            return timedelta(days=min(n * 7, 365))
        if unit == "month":
            return timedelta(days=min(n * 30, 365))

    return None


# ---------------------------------------------------------------------------
# Hard date-range extraction (pipeline path → WHERE filter)
# ---------------------------------------------------------------------------

_WORD_TO_NUMBER: dict[str, int] = {
    "one": 1,
    "a": 1,
    "an": 1,
    "two": 2,
    "couple": 2,
    "three": 3,
    "few": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

_TEMPORAL_DATE_RANGE_PATTERNS: list[tuple[str, bool]] = [
    # (pattern, is_future)
    # "a couple of weeks ago", "a few days back"
    (
        r"a\s+(?P<word>couple|few)\s+(?:of\s+)?(?P<unit>days?|weeks?|months?|years?)\s+(?P<dir>ago|back)",
        False,
    ),
    # "two months ago", "five days back"
    (
        r"(?P<word>one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+(?P<unit>days?|weeks?|months?|years?)\s+(?P<dir>ago|back)",
        False,
    ),
    # "in two weeks", "in three months"
    (
        r"in\s+(?P<word>one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|a|an|couple|few)\s+(?P<unit>days?|weeks?|months?|years?)",
        True,
    ),
    # "3 months ago", "10 days back"
    (r"(?P<num>\d+)\s+(?P<unit>days?|weeks?|months?|years?)\s+(?P<dir>ago|back)", False),
    # "last week", "last month", "last year"
    (r"last\s+(?P<unit>week|month|year)\b", False),
]

_UNIT_TO_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}

# Padding scaled to unit granularity: exact-day for "day", ±1 for "week",
# ±3 for "month", ±14 for "year". Tighter ranges shrink the candidate pool
# so the soft date-range boost in the scorer can push the right memory up.
_PAD_DAYS = {"day": 0, "week": 1, "month": 3, "year": 14}


def _extract_temporal_date_range(
    query: str,
    reference_datetime: datetime | None = None,
) -> dict[str, str] | None:
    """Extract a hard date-range filter from temporal expressions in *query*.

    Returns ``{"start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}`` or
    ``None`` when no expression is detected.

    Range padding (see ``_PAD_DAYS``): 0 days for day, ±1 day for week,
    ±3 days for month, ±14 days for year.  Pairs with the soft date-range
    boost in the storage scorer — tighter window + softer filter keeps
    out-of-range memories retrievable when semantically strong.
    """
    import re
    from datetime import UTC

    q = query.lower()
    ref = reference_datetime or datetime.now(UTC)

    for pattern, is_future in _TEMPORAL_DATE_RANGE_PATTERNS:
        m = re.search(pattern, q)
        if not m:
            continue

        groups = m.groupdict()
        unit_raw = groups.get("unit", "")
        unit = unit_raw.rstrip("s") if unit_raw else ""

        # Determine numeric value
        if "num" in groups and groups["num"] is not None:
            n = int(groups["num"])
        elif "word" in groups and groups["word"] is not None:
            n = _WORD_TO_NUMBER.get(groups["word"], 0)
        elif "last" in pattern:
            n = 1
        else:
            continue

        if n == 0:
            continue

        days_offset = n * _UNIT_TO_DAYS.get(unit, 1)
        delta = timedelta(days=days_offset)
        target = ref + delta if is_future else ref - delta

        pad = timedelta(days=_PAD_DAYS.get(unit, 3))

        start = (target - pad).date()
        end = (target + pad).date()
        return {"start_date": start.isoformat(), "end_date": end.isoformat()}

    return None


async def search_memories(
    db: AsyncSession,
    tenant_id: str,
    query: str,
    fleet_ids: list[str] | None = None,
    filter_agent_id: str | None = None,
    caller_agent_id: str | None = None,
    memory_type_filter: str | None = None,
    status_filter: str | None = None,
    valid_at: datetime | None = None,
    top_k: int = DEFAULT_SEARCH_TOP_K,
    recall_boost: bool = True,
    graph_expand: bool = True,
    tenant_config=None,
    search_profile: dict | None = None,
    diagnostic: bool = False,
    diagnostic_ctx: dict | None = None,
    readable_tenant_ids: list[str] | None = None,
) -> list[MemoryOut]:
    # Diagnostic mode requires the pipeline path for score introspection
    if _USE_PIPELINE_SEARCH or diagnostic:
        return await _search_memories_pipeline(
            db,
            tenant_id,
            query,
            fleet_ids=fleet_ids,
            filter_agent_id=filter_agent_id,
            caller_agent_id=caller_agent_id,
            memory_type_filter=memory_type_filter,
            status_filter=status_filter,
            valid_at=valid_at,
            top_k=top_k,
            recall_boost=recall_boost,
            graph_expand=graph_expand,
            tenant_config=tenant_config,
            search_profile=search_profile,
            diagnostic=diagnostic,
            diagnostic_ctx=diagnostic_ctx,
            readable_tenant_ids=readable_tenant_ids,
        )
    logger.warning("legacy search path invoked; this path is deprecated and scheduled for removal")
    return await _search_memories_legacy(
        db,
        tenant_id,
        query,
        fleet_ids=fleet_ids,
        filter_agent_id=filter_agent_id,
        caller_agent_id=caller_agent_id,
        memory_type_filter=memory_type_filter,
        status_filter=status_filter,
        valid_at=valid_at,
        top_k=top_k,
        recall_boost=recall_boost,
        graph_expand=graph_expand,
        tenant_config=tenant_config,
        search_profile=search_profile,
    )


async def _search_memories_pipeline(
    db: AsyncSession,
    tenant_id: str,
    query: str,
    fleet_ids: list[str] | None = None,
    filter_agent_id: str | None = None,
    caller_agent_id: str | None = None,
    memory_type_filter: str | None = None,
    status_filter: str | None = None,
    valid_at: datetime | None = None,
    top_k: int = DEFAULT_SEARCH_TOP_K,
    recall_boost: bool = True,
    graph_expand: bool = True,
    tenant_config=None,
    search_profile: dict | None = None,
    diagnostic: bool = False,
    diagnostic_ctx: dict | None = None,
    readable_tenant_ids: list[str] | None = None,
) -> list[MemoryOut]:
    """Pipeline-based search_memories -- same logic, decomposed into timed steps."""
    from core_api.pipeline.compositions.search import build_search_pipeline
    from core_api.pipeline.context import PipelineContext

    ctx = PipelineContext(
        db=db,
        data={
            "query": query,
            "tenant_id": tenant_id,
            "fleet_ids": fleet_ids,
            "filter_agent_id": filter_agent_id,
            "caller_agent_id": caller_agent_id,
            "memory_type_filter": memory_type_filter,
            "status_filter": status_filter,
            "valid_at": valid_at,
            "top_k": top_k,
            "recall_boost_enabled": recall_boost,
            "graph_expand": graph_expand,
            "tenant_config": tenant_config,
            "search_profile": search_profile,
            "diagnostic": diagnostic,
            "readable_tenant_ids": readable_tenant_ids,
        },
        tenant_config=tenant_config,
    )

    pipeline = build_search_pipeline()
    result = await pipeline.run(ctx)

    if result.failed:
        from core_api.pipeline.step import StepOutcome

        failed_steps = [s for s in result.steps if s.outcome == StepOutcome.FAILED]
        if failed_steps and failed_steps[-1].error:
            logger.error(
                "Search pipeline step %r failed: %s",
                failed_steps[-1],
                failed_steps[-1].error,
            )
        raise HTTPException(status_code=500, detail="Search pipeline failed unexpectedly")

    if diagnostic and diagnostic_ctx is not None:
        diagnostic_ctx["all_candidates"] = ctx.data.get("diagnostic_results", [])
        diagnostic_ctx["search_params"] = ctx.data.get("search_params", {})
        diagnostic_ctx["retrieval_strategy"] = (
            ctx.data["retrieval_plan"].strategy.value if ctx.data.get("retrieval_plan") else None
        )
        diagnostic_ctx["diagnostic_original_top_k"] = ctx.data.get("diagnostic_original_top_k")

    return ctx.data["results"]


async def _search_memories_legacy(
    db: AsyncSession,
    tenant_id: str,
    query: str,
    fleet_ids: list[str] | None = None,
    filter_agent_id: str | None = None,
    caller_agent_id: str | None = None,
    memory_type_filter: str | None = None,
    status_filter: str | None = None,
    valid_at: datetime | None = None,
    top_k: int = DEFAULT_SEARCH_TOP_K,
    recall_boost: bool = True,
    graph_expand: bool = True,
    tenant_config=None,
    search_profile: dict | None = None,
) -> list[MemoryOut]:
    """Legacy search -- uses scored_search storage API endpoint."""
    sc = get_storage_client()

    # Resolve per-agent search profile with fallback to constants
    from core_api.services.organization_settings import validate_search_profile

    sp = validate_search_profile(search_profile) if search_profile else {}
    _top_k = sp.get("top_k", top_k)
    _min_similarity = sp.get("min_similarity", MIN_SEARCH_SIMILARITY)
    _fts_weight = sp["fts_weight"] if "fts_weight" in sp else _adaptive_fts_weight(query)
    _freshness_floor = sp.get("freshness_floor", FRESHNESS_FLOOR)
    _freshness_decay_days = sp.get("freshness_decay_days", FRESHNESS_DECAY_DAYS)
    _recall_boost_cap = sp.get("recall_boost_cap", RECALL_BOOST_CAP)
    _recall_decay_window_days = sp.get("recall_decay_window_days", RECALL_DECAY_WINDOW_DAYS)
    _graph_max_hops = sp.get("graph_max_hops", GRAPH_MAX_HOPS)
    _similarity_blend = sp.get("similarity_blend", SIMILARITY_BLEND)

    # Temporal hint
    temporal_window = _extract_temporal_hint(query)

    # Parallel: embedding + entity pipeline
    emb_task = asyncio.ensure_future(_get_or_cache_embedding(query, tenant_id, tenant_config))
    ent_task = asyncio.ensure_future(
        _entity_boost_pipeline(db, query, tenant_id, fleet_ids, graph_expand, _graph_max_hops)
    )
    try:
        embedding, (boosted_memory_ids, memory_boost_factor) = await asyncio.gather(emb_task, ent_task)
    except TimeoutError:
        emb_task.cancel()
        ent_task.cancel()
        raise HTTPException(status_code=504, detail="Search embedding timed out")
    except ValueError as exc:
        emb_task.cancel()
        ent_task.cancel()
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception:
        emb_task.cancel()
        ent_task.cancel()
        raise

    # Overfetch so post-filter has headroom to drop low-vec_sim rows without
    # starving the final result set. Mirrors pipeline ExecuteScoredSearch behavior.
    _overfetch_top_k = _top_k * SEARCH_OVERFETCH_FACTOR

    # Use scored_search storage API endpoint
    search_data = {
        "tenant_id": tenant_id,
        "embedding": embedding,
        "query": query,
        "fleet_ids": fleet_ids,
        "filter_agent_id": filter_agent_id,
        "caller_agent_id": caller_agent_id,
        "memory_type_filter": memory_type_filter,
        "status_filter": status_filter,
        "valid_at": valid_at.isoformat() if valid_at else None,
        "top_k": _overfetch_top_k,
        "min_similarity": _min_similarity,
        "fts_weight": _fts_weight,
        "freshness_floor": _freshness_floor,
        "freshness_decay_days": _freshness_decay_days,
        "recall_boost_enabled": recall_boost,
        "recall_boost_cap": _recall_boost_cap,
        "recall_decay_window_days": _recall_decay_window_days,
        "similarity_blend": _similarity_blend,
        "temporal_window_days": temporal_window.days if temporal_window else None,
        "boosted_memory_ids": {str(mid): factor for mid, factor in memory_boost_factor.items()}
        if memory_boost_factor
        else None,
    }

    # CAURA-602 follow-up: per-tenant search bulkhead at the storage
    # roundtrip. The route-entry slot was already held above; this slot
    # bounds how many of one tenant's searches occupy storage-reader
    # connections simultaneously, preserving cold-tenant search latency
    # under a hot-tenant storm.
    async with per_tenant_storage_slot("storage_search", tenant_id):
        rows = await sc.scored_search(search_data)

    # Post-filter by min_similarity, then trim to top_k. The `vec_sim is None`
    # branch is defensive: the scored_search SQL currently enforces
    # `Memory.embedding IS NOT NULL` and the storage layer coerces None → 0.0,
    # so this branch is never taken today. Left in place — mirrored in the
    # pipeline post_filter step — so that FTS-only rows aren't silently gated
    # out by the cosine threshold if either invariant ever changes.
    rows = [r for r in rows if r.get("vec_sim") is None or float(r["vec_sim"]) >= _min_similarity]
    rows = rows[:_top_k]

    # Build results from storage API response
    memory_ids = [row.get("id") for row in rows if row.get("id")]

    # Fetch entity links for all result memories
    links_data = (
        await sc.get_entity_links_for_memories([str(mid) for mid in memory_ids]) if memory_ids else {}
    )

    results = []
    for row in rows:
        mid = row.get("id")
        mid_str = str(mid)
        entity_links = [
            EntityLinkOut(entity_id=el.get("entity_id"), role=el.get("role"))
            for el in links_data.get(mid_str, [])
        ]
        results.append(
            _dict_to_memory_out(
                row,
                entity_links=entity_links,
                similarity=round(float(row.get("score", 0)), 4) if row.get("score") is not None else None,
            )
        )

    # Increment recall_count and update last_recalled_at for returned memories
    _hooks = get_hooks()
    if memory_ids and _hooks.on_recall:
        try:
            await _hooks.on_recall(db, memory_ids)
        except Exception:
            logger.debug("Recall tracking hook failed (non-critical)", exc_info=True)

    return results
