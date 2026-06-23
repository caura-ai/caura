"""WriteMemoryRow — create memory via storage client, entity links, and audit log."""

from __future__ import annotations

import logging
import time

from core_api.clients.storage_client import get_storage_client
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult
from core_api.services.hooks import get_hooks

logger = logging.getLogger(__name__)


class WriteMemoryRow:
    @property
    def name(self) -> str:
        return "write_memory_row"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        data = ctx.data["input"]
        embedding = ctx.data["embedding"]
        ch = ctx.data["content_hash"]
        fields = ctx.data["memory_fields"]
        metadata = fields["metadata"]
        t0 = ctx.data.get("t0", time.perf_counter())
        # CAURA-682 Phase 1: per-phase latency capture (see
        # ParallelEmbedEnrich). ``storage_ms`` measures just the
        # ``create_memory`` roundtrip; ``entity_links_ms`` is the
        # subsequent fan-out for ``data.entity_links`` (zero links →
        # zero ms — the key is still emitted to keep field surface
        # uniform across writes).
        timings: dict = ctx.data.setdefault("phase_timings", {})

        if embedding is None:
            metadata["embedding_pending"] = True
            logger.warning("Storing memory without embedding; deferred backfill scheduled")

        # Store write latency in metadata. Despite the name, this is
        # pipeline-start-to-pre-storage, not the storage call duration —
        # kept as-is because metadata consumers (audit log, dashboard)
        # depend on the contract. ``timings["storage_ms"]`` below is
        # the new, accurately-named signal for Phase 1 measurement.
        write_ms = round((time.perf_counter() - t0) * 1000)
        metadata["write_latency_ms"] = write_ms

        sc = get_storage_client()
        memory_data = {
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
            # Pass the dict through. ``write_latency_ms`` is always
            # added at line 35, so ``metadata`` is never falsy here —
            # the previous ``or None`` was dead code that, if ever
            # reachable, would coerce an intentional ``{}`` to NULL,
            # the same falsy-``{}`` trap fixed across the read path.
            # Stored as ``{}`` (not NULL) is the canonical "no
            # metadata" representation; no SQL ``IS NULL`` filters
            # exist on this column.
            "metadata_": metadata,
            "content_hash": ch,
            "expires_at": str(data.expires_at) if data.expires_at else None,
            "subject_entity_id": str(data.subject_entity_id) if data.subject_entity_id else None,
            "predicate": data.predicate,
            "object_value": data.object_value,
            "ts_valid_start": str(fields["ts_valid_start"]) if fields.get("ts_valid_start") else None,
            "ts_valid_end": str(fields["ts_valid_end"]) if fields.get("ts_valid_end") else None,
            "status": fields["status"],
            "visibility": data.visibility or "scope_team",
        }
        storage_t0 = time.perf_counter()
        memory = await sc.create_memory(memory_data)
        timings["storage_ms"] = round((time.perf_counter() - storage_t0) * 1000)

        links_t0 = time.perf_counter()
        for link in data.entity_links:
            await sc.create_entity_link(
                {
                    "memory_id": memory["id"],
                    # Stringify the UUID for JSON transport — mirrors
                    # line 60's handling of ``subject_entity_id`` and
                    # the bulk write path. SQLAlchemy auto-coerces on
                    # receive, so the persisted value is identical.
                    "entity_id": str(link.entity_id),
                    "role": link.role,
                }
            )
        timings["entity_links_ms"] = round((time.perf_counter() - links_t0) * 1000)

        detail = {
            "memory_type": fields["memory_type"],
            "title": fields["title"],
            "content_length": len(data.content),
            "write_latency_ms": write_ms,
        }

        _hooks = get_hooks()
        if _hooks.audit_log:
            try:
                await _hooks.audit_log(
                    ctx.db,  # log_action ignores db (storage-routed) — allow STM path
                    tenant_id=data.tenant_id,
                    agent_id=data.agent_id,
                    action="create",
                    resource_type="memory",
                    resource_id=memory["id"],
                    detail=detail,
                )
            except Exception:
                logger.warning("Audit hook failed (non-critical)", exc_info=True)

        ctx.data["memory"] = memory
        ctx.data["memory_id"] = memory["id"]
        return None
