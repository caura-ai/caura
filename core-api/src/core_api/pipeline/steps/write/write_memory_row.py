"""WriteMemoryRow — create memory via storage client, entity links, and audit log."""

from __future__ import annotations

import logging
import time

from fastapi import HTTPException

from common import duplicate_memory
from core_api.clients.storage_client import DuplicateMemoryError, get_storage_client
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult
from core_api.services.hooks import get_hooks

logger = logging.getLogger(__name__)

#: Per-link ERROR lines emitted per request before falling back to the single
#: summary below. ``entity_links`` has no schema bound, so this is what keeps
#: log volume from tracking caller input.
_MAX_LINK_ERROR_LOGS = 5


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
        try:
            memory = await sc.create_memory(memory_data)
        except DuplicateMemoryError as exc:
            # Migration 040's unique index rejected the insert. ``CheckExactDuplicate``
            # ran earlier in this same pipeline and found nothing, so reaching here
            # means a concurrent writer committed the same content in between — the
            # one duplicate case a check-then-insert gate cannot see.
            #
            # 409, the same code and shape that gate raises, because it is the same
            # answer: the content is already stored, here is the row. Without this
            # the step would be marked FAILED and the caller would get "Memory
            # write pipeline failed unexpectedly" — a 500 for a completely ordinary
            # race, and one that says nothing about which row to use instead.
            #
            # Nothing has been committed by THIS request at this point, so unlike
            # everything below, raising here is correct rather than a strand.
            raise HTTPException(
                status_code=409,
                detail=duplicate_memory.core_api_detail(str(exc), **exc.fields),
            ) from exc
        timings["storage_ms"] = round((time.perf_counter() - storage_t0) * 1000)

        # H-05: the row above is COMMITTED, so everything after it degrades rather
        # than raising. A raise here marks the pipeline FAILED and breaks before
        # ``ScheduleBackgroundTasks`` — which is what schedules the embed and
        # enrichment backfill — so the caller got a 500 for a write that persisted
        # and the row was left unreachable and unrepairable. The test in
        # tests/pipeline/test_write_pipeline.py carries the full incident.
        #
        # ``entity_links`` are caller-supplied UUIDs with no upstream existence
        # check, so one bad id is an FK violation → storage 500 → HTTPStatusError.
        # Per-link rather than one try around the loop: a single bad id must not
        # discard the valid links beside it.
        links_t0 = time.perf_counter()
        linked: list = []
        link_failures: list[dict] = []
        for link in data.entity_links:
            try:
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
            except Exception as exc:
                # Still degrade in EVERY case — the row is committed, and letting
                # anything propagate here is exactly the H-05 bug. But the two
                # causes need different operator responses, so they are not
                # logged identically:
                #
                #   * 4xx — the caller named a memory/entity that does not exist,
                #     or a pair already linked. Permanent, their input, one link.
                #   * anything else (5xx, connect, timeout) — storage's problem,
                #     ours to chase. If it hits every link in the request it is an
                #     outage, not N independent data-loss events, and an outage
                #     with zero failed requests is the worst kind to diagnose.
                #
                # This distinction only became possible once the links route
                # started returning 409 for a bad id; before that an FK violation
                # and an unreachable storage service were both a bare 500.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                permanent = status is not None and 400 <= status < 500
                link_failures.append(
                    {"entity_id": str(link.entity_id), "role": link.role, "permanent": permanent}
                )
                if len(link_failures) <= _MAX_LINK_ERROR_LOGS:
                    # ERROR, not warning: unlike the audit hook, a dropped link is
                    # user-visible data loss — the memory exists but is not
                    # reachable through that entity, and a link that was never
                    # created leaves no row for ``GET /entities/broken-links`` to
                    # find later. The row itself is fine, so the write stands.
                    logger.error(
                        "entity link failed; memory kept without it",
                        exc_info=True,
                        extra={
                            "memory_id": memory["id"],
                            "tenant_id": data.tenant_id,
                            "entity_id": str(link.entity_id),
                            "role": link.role,
                            "error_type": type(exc).__name__,
                            "status_code": status,
                            # False → chase storage, not the caller.
                            "permanent": permanent,
                        },
                    )
                continue
            linked.append(link)
        timings["entity_links_ms"] = round((time.perf_counter() - links_t0) * 1000)
        # Read by ``_memory_out_with_created_links``, which echoes these rather
        # than the request so the caller is told what actually persisted.
        ctx.data["entity_links_created"] = linked
        if link_failures:
            ctx.data["entity_link_failures"] = link_failures
            transient = sum(1 for f in link_failures if not f["permanent"])
            if len(link_failures) > _MAX_LINK_ERROR_LOGS or transient:
                # One summary, because ``entity_links`` is unbounded: a caller
                # sending a thousand bad ids would otherwise emit a thousand ERROR
                # lines, and log volume proportional to caller input is a
                # denial-of-observability. Also fires whenever ANY failure was
                # transient — that is the outage signal, and it must not be the
                # thing the cap swallowed.
                logger.error(
                    "entity links dropped: %d of %d (%d transient)",
                    len(link_failures),
                    len(data.entity_links),
                    transient,
                    extra={
                        "memory_id": memory["id"],
                        "tenant_id": data.tenant_id,
                        "dropped": len(link_failures),
                        "requested": len(data.entity_links),
                        "transient": transient,
                        "logged_individually": min(len(link_failures), _MAX_LINK_ERROR_LOGS),
                    },
                )

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
                    # log_action is keyword-only since #491 dropped the direct DB
                    # pool (storage-routed); do NOT pass ctx.db positionally.
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
