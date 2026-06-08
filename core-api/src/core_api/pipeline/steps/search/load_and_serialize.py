"""LoadAndSerialize — serialize results to MemoryOut using pre-loaded entity links."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from core_api.clients.storage_client import get_storage_client
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult
from core_api.services.memory_service import _memory_to_out

logger = logging.getLogger(__name__)

MAX_SUCCESSOR_LOOKUPS = 10


class LoadAndSerialize:
    @property
    def name(self) -> str:
        return "load_and_serialize"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        rows = list(ctx.data["filtered_rows"])

        # Follow supersedes chain: inject successors for outdated/conflicted memories
        outdated_ids = [
            row.Memory.id if hasattr(row.Memory, "id") else row.Memory.get("id")
            for row in rows
            if (row.Memory.status if hasattr(row.Memory, "status") else row.Memory.get("status"))
            in ("outdated", "conflicted")
        ]
        if outdated_ids:
            if len(outdated_ids) > MAX_SUCCESSOR_LOOKUPS:
                logger.warning(
                    "Capping successor lookups from %d to %d",
                    len(outdated_ids),
                    MAX_SUCCESSOR_LOOKUPS,
                )
                outdated_ids = outdated_ids[:MAX_SUCCESSOR_LOOKUPS]
            existing_ids = {
                str(row.Memory.id if hasattr(row.Memory, "id") else row.Memory.get("id")) for row in rows
            }
            data = ctx.data
            tenant_id = data["tenant_id"]

            sc = get_storage_client()
            try:
                successors = await sc.find_successors(
                    {
                        "supersedes_ids": [str(oid) for oid in outdated_ids],
                        "tenant_id": tenant_id,
                        "fleet_ids": data.get("fleet_ids"),
                        "caller_agent_id": data.get("caller_agent_id"),
                        "filter_agent_id": data.get("filter_agent_id"),
                        "memory_type_filter": data.get("memory_type_filter"),
                        "valid_at": str(data["valid_at"]) if data.get("valid_at") else None,
                    }
                )
            except Exception:
                logger.warning(
                    "find_successors failed; continuing without successor enrichment", exc_info=True
                )
                successors = []
            for successor in successors:
                sid = successor.get("id")
                if sid not in existing_ids:
                    rows.append(
                        SimpleNamespace(
                            Memory=SimpleNamespace(**successor),
                            score=None,
                            similarity=None,
                            vec_sim=None,
                            fts_score=None,
                            freshness=None,
                            entity_boost=None,
                            recall_boost=None,
                            temporal_boost=None,
                            status_penalty=None,
                            entity_links=[],
                        )
                    )
                    existing_ids.add(sid)

        ctx.data["results"] = [
            _memory_to_out(
                row.Memory,
                entity_links=row.entity_links,
                similarity=(round(float(row.score), 4) if row.score is not None else None),
            )
            for row in rows
        ]
        return None
