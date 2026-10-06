"""CreatePendingSubject — create a new identifier subject once its row exists (L-18).

``EmitMemoryTriple`` used to upsert an identifier subject's entity itself. It
runs before the semantic gate in strong mode and before ``WriteMemoryRow`` in
every pipeline, so a write refused afterwards (a semantic-duplicate 409, or
the concurrent-insert 409 in ``WriteMemoryRow``) left an entity no memory
references, listable through ``/entities`` and ``/graph``.

It now looks the identifier up only and leaves a miss in
``ctx.data[PENDING_SUBJECT]``. This step runs right after ``WriteMemoryRow``,
so it is reached only for a row that exists. It creates the entity, writes
subject, predicate and object to the row in one update, and mirrors them onto
the request and the stored-row dict, before ``ScheduleBackgroundTasks`` reads
them for contradiction detection.

Never breaks the write. If the create or the update fails, the row keeps no
triple, which is what ``EmitMemoryTriple`` produced on the same failure when
the upsert lived there. An update that fails after the create leaves the entity
unreferenced, the one window left; the warning names it.
"""

from __future__ import annotations

import logging

from core_api.clients.storage_client import get_storage_client
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult
from core_api.pipeline.steps.write.emit_memory_triple import PENDING_SUBJECT
from core_api.schemas import EntityUpsert
from core_api.services.entity_service import upsert_entity

logger = logging.getLogger(__name__)


class CreatePendingSubject:
    @property
    def name(self) -> str:
        return "create_pending_subject"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        pending = ctx.data.get(PENDING_SUBJECT)
        if not pending:
            return StepResult(outcome=StepOutcome.SKIPPED)
        data = ctx.data["input"]
        name = pending["canonical_name"]
        try:
            entity = await upsert_entity(
                EntityUpsert(
                    tenant_id=data.tenant_id,
                    fleet_id=data.fleet_id,
                    entity_type="identifier",
                    canonical_name=name,
                ),
            )
        except Exception as exc:
            logger.warning("Subject-inference create failed for %r: %s", name, exc)
            return StepResult(outcome=StepOutcome.SKIPPED, detail={"reason": "subject_upsert_failed"})
        triple = {
            "subject_entity_id": str(entity.id),
            "predicate": pending["predicate"],
            "object_value": pending["object_value"],
        }
        memory_id = str(ctx.data["memory_id"])
        # Unconditional: the row was written a moment ago by this request, and
        # the tasks that could also write these columns are scheduled by the
        # step after this one.
        try:
            stored = await get_storage_client().update_memory(memory_id, data.tenant_id, triple)
            # ``update_memory`` answers None, not an error, for a row that is
            # gone (a 404): deleted since this request wrote it.
            failure = "memory is gone" if stored is None else None
        except Exception as exc:
            failure = repr(exc)
        if failure is not None:
            # Nothing was stored, so nothing is reported. The entity above is
            # already committed and may now be unreferenced (or was merged into
            # one that already existed), so name it for whoever cleans up.
            logger.warning(
                "Pending subject %r not written: entity %s, tenant %s, memory %s: %s",
                name,
                entity.id,
                data.tenant_id,
                memory_id,
                failure,
            )
            return StepResult(outcome=StepOutcome.SKIPPED, detail={"reason": "subject_update_failed"})
        data.subject_entity_id = entity.id
        data.predicate = pending["predicate"]
        data.object_value = pending["object_value"]
        memory = ctx.data.get("memory")
        if isinstance(memory, dict):
            memory.update(triple)
        logger.info("emit_triple created pending subject=%s predicate=%s", name, pending["predicate"])
        return None
