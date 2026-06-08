"""Event-driven re-embedding of memories whose embedding is NULL.

After alembic migration ``012_vector_dim_1024`` NULLs every 768-dim
embedding to widen the column to 1024-dim, retrieval is broken for
those rows until they're re-embedded. This task drives the existing
``handle_embed_request`` consumer by publishing one
``Topics.Memory.EMBED_REQUESTED`` event per NULL row.

Two backfills exist by design:

* This task — event-driven, runs in ``core-worker`` against the same
  Pub/Sub bus as production traffic. Recommended for enterprise /
  multi-tenant production cutovers because it inherits the consumer's
  per-tenant concurrency, retry, and DLQ wiring.
* A standalone script at ``core-storage-api/src/core_storage_api/scripts/
  backfill_embeddings.py`` — direct DB writes, no event bus needed.
  Recommended for OSS docker-compose deployments where the worker may
  not be running.

Design notes:

* Idempotent: re-running picks up only still-NULL rows. The consumer's
  writes flip them non-NULL, so a restart resumes naturally.
* Backpressure: ``max_inflight`` caps the number of unacknowledged
  publishes. Tune up for throughput, down to avoid swamping the
  embedding provider or the consumer's per-tenant slots.
* No retry / DLQ logic here — that's the consumer's job
  (``handle_embed_request`` already raises → Pub/Sub redelivers →
  max-delivery-attempts → DLQ).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from common.events.memory_embed_publisher import publish_memory_embed_request
from core_worker.clients.storage_client import (
    NullEmbeddingRow,
    get_memory,
    get_storage_client,
    iter_memories_with_null_embedding,
)

logger = logging.getLogger(__name__)


@dataclass
class BackfillReport:
    scanned: int
    published: int
    skipped_missing: int
    elapsed_s: float


async def run_embedding_backfill(
    *,
    tenant_id: str,
    batch_size: int = 500,
    max_inflight: int = 100,
    dry_run: bool = False,
) -> BackfillReport:
    """Scan NULL-embedding memories for one tenant, publish ``EMBED_REQUESTED``.

    Parameters
    ----------
    tenant_id:
        **Required.** Scope the scan to a single tenant. The
        storage-API endpoint refuses un-scoped calls because the OSS
        storage API has no auth middleware and this path returns raw
        memory content. For whole-deployment cutovers, the operator
        iterates over the tenant list and invokes this function once
        per tenant — which is also the documented prod-cutover pattern
        (per-tenant phasing limits blast radius).
    batch_size:
        How many ids to fetch per storage-API page.
    max_inflight:
        Concurrency cap on outstanding publishes. Higher = faster but
        risks event-bus overload and consumer lag.
    dry_run:
        If True, count rows that would be published. Don't publish.
    """
    import httpx

    sem = asyncio.Semaphore(max_inflight)
    scanned = 0
    published = 0
    skipped_missing = 0
    started = time.monotonic()

    async def _publish_one(row: NullEmbeddingRow) -> None:
        nonlocal published, skipped_missing
        async with sem:
            # Fetch the memory's content + content_hash by id. The
            # listing endpoint returns ids-only (defence-in-depth on
            # the unauthenticated storage API), so the worker has to
            # round-trip per row to read the content. With
            # ``max_inflight`` concurrency this overlaps fully with
            # publishing — net throughput is bounded by the storage
            # API + event bus, not by the extra hop.
            try:
                memory = await get_memory(storage, memory_id=row.memory_id, tenant_id=row.tenant_id)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    # Row was soft-deleted (or hard-deleted) between
                    # the listing scan and this fetch. Don't fail the
                    # backfill — just skip; nothing to re-embed.
                    skipped_missing += 1
                    return
                raise
            content = memory.get("content")
            if not content:
                # ``content`` is NOT NULL on memories, but defensive
                # against a future schema where it might not be —
                # skipping is safer than publishing a degenerate
                # embed request the consumer would reject.
                skipped_missing += 1
                return
            if not dry_run:
                await publish_memory_embed_request(
                    memory_id=row.memory_id,
                    content=content,
                    tenant_id=row.tenant_id,
                    content_hash=memory.get("content_hash"),
                )
            published += 1

    storage = get_storage_client()
    async for batch in iter_memories_with_null_embedding(storage, tenant_id=tenant_id, batch_size=batch_size):
        scanned += len(batch)
        await asyncio.gather(*(_publish_one(row) for row in batch))
        logger.info(
            "embedding_backfill progress: scanned=%d published=%d skipped=%d",
            scanned,
            published,
            skipped_missing,
        )

    elapsed = time.monotonic() - started
    logger.info(
        "embedding_backfill done: scanned=%d published=%d skipped=%d elapsed=%.1fs",
        scanned,
        published,
        skipped_missing,
        elapsed,
    )
    return BackfillReport(
        scanned=scanned,
        published=published,
        skipped_missing=skipped_missing,
        elapsed_s=elapsed,
    )
