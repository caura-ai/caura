"""TrackRecalls — fire-and-forget recall tracking in a background task.

Routes the recall_count UPDATE through core-storage (no core-api DB pool) in a
background task so the search response returns immediately without waiting for
the HTTP round-trip.
"""

from __future__ import annotations

import logging
from uuid import UUID

from core_api.clients.storage_client import get_storage_client
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepResult
from core_api.tasks import track_task

logger = logging.getLogger(__name__)


async def _track_recalls_background(memory_ids: list[UUID], *, tenant_id: str) -> None:
    """Background task: bump recall stats via the storage client.

    ``memory_ids`` are stringified for the JSON payload (``_post`` does not
    auto-encode UUIDs); the storage endpoint re-parses each as a UUID.
    """
    try:
        await get_storage_client().increment_recall(
            [str(m) for m in memory_ids],
            tenant_id=tenant_id,
        )
    except Exception:
        logger.warning("Background recall tracking failed", exc_info=True)


class TrackRecalls:
    @property
    def name(self) -> str:
        return "track_recalls"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        # ``recall_count`` is a per-memory "an agent found this useful" signal,
        # and it feeds ``recall_boost`` in scoring. Only bump it for recalls
        # that carry a caller agent identity. Agentless ``/search`` traffic —
        # liveness/health probes, monitoring pollers, dashboard/admin queries
        # that hit the endpoint with no agent — is not an agent using memory;
        # counting it inflates ``recall_count`` (and thus ``recall_boost``) with
        # non-agent noise and can pin a single memory under a repeating probe.
        # ``caller_agent_id`` is the authenticated identity (``filter_agent_id``
        # or ``auth.agent_id``), so genuine agent recalls — including cross-agent
        # ones that omit ``filter_agent_id`` — still count. Results are returned
        # unchanged either way; only the counter bump is skipped. (Gap A26.)
        #
        # ``recall_tracked`` is set on every path below so the surfaces can
        # report what this search actually did rather than re-deriving the
        # policy at the route. A re-derived predicate would already be wrong:
        # the legacy ``_search_memories_legacy`` path bumps unconditionally,
        # and the "no rows" case below is invisible from the caller's inputs.
        ctx.data["recall_tracked"] = False
        if not ctx.data.get("caller_agent_id"):
            return None
        # D12 — a diagnostic call is inspection, not use: the caller is asking
        # "why does ranking look like this", and letting that bump
        # ``recall_count`` would distort the very signal being inspected (the
        # A26/A48 class of feedback loop). This also makes ``diagnostic=true``
        # the supported dry-run recall: same results, no reinforcement.
        if ctx.data.get("diagnostic"):
            return None
        memory_ids = [row.Memory.id for row in ctx.data["filtered_rows"]]
        if memory_ids:
            track_task(
                _track_recalls_background(
                    memory_ids,
                    tenant_id=ctx.data["tenant_id"],
                )
            )
            # Dispatched, not completed — the bump is fire-and-forget, so this
            # reports "this search asked for a bump", which is the fact a
            # caller needs to tell a pinned-at-zero counter from a working one.
            ctx.data["recall_tracked"] = True
        return None
