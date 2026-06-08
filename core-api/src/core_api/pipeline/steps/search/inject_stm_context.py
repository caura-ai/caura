"""InjectSTMContext — prepend STM notes and bulletin entries to search results."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult

logger = logging.getLogger(__name__)


class InjectSTMContext:
    @property
    def name(self) -> str:
        return "inject_stm_context"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        from core_api.config import settings

        if not settings.use_stm:
            return StepResult(outcome=StepOutcome.SKIPPED)

        from core_api.schemas import MemoryOut
        from core_api.services.stm_service import get_stm_backend_instance

        tenant_id = ctx.data["tenant_id"]
        caller_agent_id = ctx.data.get("caller_agent_id")
        fleet_ids = ctx.data.get("fleet_ids")

        stm = get_stm_backend_instance()
        stm_results: list[MemoryOut] = []

        # Agent's private notes
        if caller_agent_id:
            notes = await stm.get_notes(tenant_id, caller_agent_id, limit=50)
            for entry in notes:
                stm_results.append(_entry_to_memory_out(entry, tenant_id, "notes"))

        # Fleet bulletins (deduplicated by content across fleets)
        if fleet_ids:
            bulletin_entries: list[dict] = []
            for fid in fleet_ids:
                bulletin_entries.extend(await stm.get_bulletin(tenant_id, fid, limit=100))
            seen: set[str] = set()
            bulletin_entries = [
                e
                for e in bulletin_entries
                if not (e.get("content", "") in seen or seen.add(e.get("content", "")))  # type: ignore[func-returns-value]
            ]
            for entry in bulletin_entries:
                stm_results.append(_entry_to_memory_out(entry, tenant_id, "bulletin"))

        if stm_results:
            existing = ctx.data.get("results", [])
            ctx.data["results"] = stm_results + existing
            logger.info("Injected %d STM entries into search results", len(stm_results))

        return None


def _entry_to_memory_out(entry: dict, tenant_id: str, stm_target: str):
    """Convert an STM entry dict to a synthetic MemoryOut."""
    from core_api.schemas import MemoryOut

    # Deterministic UUID from content for dedup
    content = entry.get("content", "")
    entry_id = entry.get("id", "")
    uid = uuid.uuid5(uuid.NAMESPACE_URL, f"stm:{entry_id}:{content}")

    posted_at = entry.get("posted_at")
    if isinstance(posted_at, str):
        try:
            created_at = datetime.fromisoformat(posted_at)
        except (ValueError, TypeError):
            created_at = datetime.now(UTC)
    else:
        created_at = datetime.now(UTC)

    return MemoryOut(
        id=uid,
        tenant_id=tenant_id,
        agent_id=entry.get("agent_id", "unknown"),
        memory_type="stm",
        content=content,
        weight=1.0,
        source_uri=None,
        run_id=None,
        metadata={"source": "stm", "stm_target": stm_target},
        created_at=created_at,
        expires_at=None,
        similarity=1.0,
    )
