"""WriteMemoryRow — create memory via storage client, entity links, and audit log."""

from __future__ import annotations

import logging
import time

from fastapi import HTTPException

from common import duplicate_memory
from core_api.clients.storage_client import DuplicateMemoryError, get_storage_client
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult
from core_api.schemas import EntityLinkIn
from core_api.services.hooks import get_hooks
from core_api.services.system_metadata import set_system_value

logger = logging.getLogger(__name__)

#: Per-link ERROR lines emitted per request before falling back to the single
#: summary below. ``entity_links`` has no schema bound, so this is what keeps
#: log volume from tracking caller input.
_MAX_LINK_ERROR_LOGS = 5
#: Router-enforced cap on ``POST /entities/links/bulk`` (see
#: ``bulk_upsert_memory_entity_links``). Kept equal to it, not lower: a smaller
#: value only adds round-trips, and a larger one turns an oversized write into a
#: 422 that loses every link in the chunk.
_LINK_BULK_CHUNK = 500


def _record_link_failure(
    link_failures: list[dict],
    link: EntityLinkIn,
    permanent: bool,
    memory_id: object,
    tenant_id: str,
    *,
    exc: Exception | None = None,
    error: str | None = None,
) -> None:
    """Record one dropped entity link, logging the first few individually.

    Split out when the per-link loop became a bulk call (OSS 08/14 L-34) so the
    two ways a link can now fail — the chunk's call raising, and the item coming
    back with an ``error`` — record and log identically. They are the same event
    to an operator: the memory exists and is not reachable through that entity.

    ERROR, not warning, for the reason the original carried: unlike the audit
    hook, a dropped link is user-visible data loss, and a link that was never
    created leaves no row for ``GET /entities/broken-links`` to find later. The
    row itself is fine, so the write stands.
    """
    link_failures.append({"entity_id": str(link.entity_id), "role": link.role, "permanent": permanent})
    if len(link_failures) > _MAX_LINK_ERROR_LOGS:
        return
    status = getattr(getattr(exc, "response", None), "status_code", None)
    logger.error(
        "entity link failed; memory kept without it",
        exc_info=exc is not None,
        extra={
            "memory_id": memory_id,
            "tenant_id": tenant_id,
            "entity_id": str(link.entity_id),
            "role": link.role,
            "error_type": type(exc).__name__ if exc is not None else error,
            "status_code": status,
            # False → chase storage, not the caller.
            "permanent": permanent,
        },
    )


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
            set_system_value(metadata, "embedding_pending", True)
            logger.warning("Storing memory without embedding; deferred backfill scheduled")

        # Store write latency in metadata. Despite the name, this is
        # pipeline-start-to-pre-storage, not the storage call duration —
        # kept as-is because metadata consumers (audit log, dashboard)
        # depend on the contract. ``timings["storage_ms"]`` below is
        # the new, accurately-named signal for Phase 1 measurement.
        write_ms = round((time.perf_counter() - t0) * 1000)
        set_system_value(metadata, "write_latency_ms", write_ms)

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
            # A62 — migration 036 added this column and nothing ever wrote it, so
            # every row read ``False`` = "directly stated". That silently disabled
            # the invariant at ``resolution.py``: ``if is_inferred and action in
            # _DESTRUCTIVE`` exists so a memory the SYSTEM materialised cannot
            # destructively overturn one a user actually stated — and with the
            # column always False it has never once fired.
            #
            # Server-set only. It is absent from ``MemoryCreate`` (it lives on
            # ``MemoryOut``), so a caller cannot claim to be inferred, nor claim
            # not to be; the value comes from ``create_memory``'s internal
            # keyword, which only platform writers pass.
            "is_inferred": bool(ctx.data.get("is_inferred", False)),
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
        # OSS 08/14 L-34 — one bulk round-trip per 500 links, not one per link.
        # A write naming 40 entities used to make 40 sequential HTTP calls to
        # core-storage-api on the inline write path, with the caller blocked on
        # all of them.
        #
        # The per-link loop this replaces was deliberate, and its reason (the
        # H-05 incident: "a single bad id must not discard the valid links
        # beside it") is preserved rather than traded away — because
        # ``entity_bulk_upsert_links`` was built with the same requirement. It
        # runs each item in its OWN session precisely so an FK violation on item
        # N cannot roll back items 0..N-1, and reports the failure per item as
        # ``error="fk_violation"`` aligned by ``input_idx``. Batching the links
        # is therefore not the trade the fan-out's per-fact CREATE would be,
        # where the bulk helper is all-or-nothing.
        #
        # Chunked at the router's documented 500-item cap because
        # ``entity_links`` is unbounded: a 501-link write would otherwise come
        # back 422 for the whole request and lose every link, which is exactly
        # the failure mode this loop exists to prevent.
        for chunk_start in range(0, len(data.entity_links), _LINK_BULK_CHUNK):
            chunk = data.entity_links[chunk_start : chunk_start + _LINK_BULK_CHUNK]
            items = [
                {
                    "input_idx": idx,
                    "memory_id": memory["id"],
                    # Stringify the UUID for JSON transport — mirrors line 60's
                    # handling of ``subject_entity_id``. SQLAlchemy auto-coerces
                    # on receive, so the persisted value is identical.
                    "entity_id": str(link.entity_id),
                    "role": link.role,
                }
                for idx, link in enumerate(chunk)
            ]
            try:
                results = await sc.bulk_upsert_entity_links(data.tenant_id, items)
            except Exception as exc:
                # Still degrade in EVERY case — the row is committed, and letting
                # anything propagate here is exactly the H-05 bug. The whole
                # chunk is lost rather than one link, which is the one place the
                # batching does change behaviour; the classification below is
                # what keeps that readable. A transport failure that takes out a
                # chunk is an outage, and the old comment already said an outage
                # is not N independent data-loss events.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                permanent = status is not None and 400 <= status < 500
                for link in chunk:
                    _record_link_failure(
                        link_failures, link, permanent, memory["id"], data.tenant_id, exc=exc
                    )
                continue

            # Aligned by ``input_idx`` rather than by position: the response is
            # documented as aligned to input, but the id is what makes that a
            # checked property instead of an assumption.
            by_idx = {r.get("input_idx"): r for r in results}
            for idx, link in enumerate(chunk):
                res = by_idx.get(idx)
                if res is None or res.get("error"):
                    # ``fk_violation`` is the caller naming a memory or entity
                    # that does not exist or is not theirs — permanent, their
                    # input, one link, same verdict the 4xx branch gave it
                    # before. A missing slot is storage not answering for an
                    # item it was asked about; treated the same way, since the
                    # link is equally not there.
                    _record_link_failure(
                        link_failures,
                        link,
                        True,
                        memory["id"],
                        data.tenant_id,
                        error=(res or {}).get("error", "missing_result"),
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
