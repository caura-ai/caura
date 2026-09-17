"""WriteSTMNote — post the entry to the STM backend and build the response."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult


class WriteSTMNote:
    @property
    def name(self) -> str:
        return "write_stm_note"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        from core_api.config import settings
        from core_api.schemas import STMWriteResponse
        from core_api.services.stm_service import get_stm_backend_instance

        data = ctx.data["input"]
        target = ctx.data["stm_target"]
        stm = get_stm_backend_instance()

        now = datetime.now(UTC)
        entry_id = str(uuid4())
        entry = {
            "id": entry_id,
            "agent_id": data.agent_id,
            "content": data.content,
            "memory_type": data.memory_type or "fact",
            "metadata": data.metadata or {},
            "posted_at": now.isoformat(),
        }

        if target == "notes":
            stored = await stm.post_note(data.tenant_id, data.agent_id, entry)
            ttl = settings.stm_notes_ttl
        else:
            fleet_id = ctx.data.get("stm_fleet_id") or data.fleet_id or "default"
            stored = await stm.post_bulletin(data.tenant_id, fleet_id, entry)
            ttl = settings.stm_bulletin_ttl

        # The receipt below is only true if the entry is actually in the
        # backend. This step used to build it unconditionally: a fresh uuid, a
        # TTL read from settings, and 201 — for a write the backend had
        # silently dropped because Redis was unreachable. The caller was left
        # holding an id that resolves to nothing, and a 2xx is the one answer
        # that stops it retrying.
        #
        # 503, not 500: nothing about the request was wrong and re-sending it
        # is the right move once the backend is back. ``STATUS_TO_CODE`` maps
        # it to UNAVAILABLE, which says that.
        if not stored:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Short-term memory is temporarily unavailable and the entry "
                    "was not stored. Retry shortly; nothing was written."
                ),
            )

        t0 = ctx.data.get("t0", time.perf_counter())
        latency_ms = round((time.perf_counter() - t0) * 1000)

        ctx.data["stm_response"] = STMWriteResponse(
            id=entry_id,
            write_mode="stm",
            target=target,
            tenant_id=data.tenant_id,
            agent_id=data.agent_id,
            content=data.content,
            ttl=ttl,
            posted_at=now,
            latency_ms=latency_ms,
        )
        return None
