"""CheckExactDuplicate — reject if content_hash already exists for the
same (tenant, fleet, agent). Stage 5: per-agent dedup so cross-agent
writes of identical content no longer collide (friction §2.8)."""

from __future__ import annotations

import time

from fastapi import HTTPException

from common import duplicate_memory
from core_api.clients.storage_client import get_storage_client
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult


class CheckExactDuplicate:
    @property
    def name(self) -> str:
        return "check_exact_duplicate"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        data = ctx.data["input"]
        ch = ctx.data["content_hash"]

        sc = get_storage_client()
        # Time just the storage roundtrip so write-latency attribution can
        # separate the dedup lookup (GET /memories/by-content-hash) from the
        # insert (``storage_ms``) and from core-api-side overhead. Mirrors the
        # ``storage_ms`` / ``entity_links_ms`` timing in WriteMemoryRow.
        dedup_t0 = time.perf_counter()
        dup = await sc.find_by_content_hash(
            data.tenant_id,
            ch,
            fleet_id=data.fleet_id,
            agent_id=data.agent_id,
        )
        ctx.data.setdefault("phase_timings", {})["dedup_lookup_ms"] = round(
            (time.perf_counter() - dedup_t0) * 1000
        )
        if dup:
            # The message is unchanged; the fields beside it are the point (C29).
            # ``status`` in particular was never expressible in the sentence, and
            # "you duplicated an archived row" needs a different response from
            # "you duplicated a live one".
            raise HTTPException(
                status_code=409,
                detail=duplicate_memory.core_api_detail(
                    duplicate_memory.exact_message(dup["id"]),
                    **duplicate_memory.duplicate_fields(
                        reason=duplicate_memory.REASON_EXACT,
                        existing_id=dup["id"],
                        existing_status=dup.get("status"),
                    ),
                ),
            )
        return None
