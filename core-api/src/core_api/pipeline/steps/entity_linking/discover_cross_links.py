"""DiscoverCrossLinks — link under-connected memories to similar entities.

DB-free as of Fix 2 Ph6: the candidate-find, pgvector LATERAL match, text-verify
filter, and bulk ON-CONFLICT insert all run in ONE atomic core-storage-api
transaction behind ``POST /entities/discover-cross-links``. This step reads
tuning from ``ctx.data`` + ``core_api.constants`` and calls the storage client.
"""

from __future__ import annotations

import asyncio
import logging

from core_api.clients.storage_client import get_storage_client
from core_api.config import settings
from core_api.constants import (
    CROSS_LINK_MEMORY_BATCH_SIZE,
    CROSS_LINK_SIMILARITY_THRESHOLD,
    CROSS_LINK_TEXT_VERIFY,
)
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult

logger = logging.getLogger(__name__)


class DiscoverCrossLinks:
    @property
    def name(self) -> str:
        return "discover_cross_links"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        """Find active memories with few entity links and connect them to similar entities."""
        tenant_id: str = ctx.data["tenant_id"]
        fleet_id: str | None = ctx.data.get("fleet_id")
        batch_size: int = ctx.data.get("cross_link_memory_batch_size", CROSS_LINK_MEMORY_BATCH_SIZE)
        threshold: float = ctx.data.get("cross_link_similarity_threshold", CROSS_LINK_SIMILARITY_THRESHOLD)
        text_verify: bool = ctx.data.get("cross_link_text_verify", CROSS_LINK_TEXT_VERIFY)
        target_memory_ids: list | None = ctx.data.get("target_memory_ids")

        # Budgeted so THIS cancellation wins, not the storage client's 120s
        # httpx read and not the writer's 120s Cloud Run timeout -- those two
        # are equal to each other, so before this the loser was whichever
        # fired first and the caller got an unexplained `ReadTimeout('')`.
        # See ``cross_link_request_timeout_seconds``; same shape as the bulk
        # route's ``asyncio.wait_for`` (CAURA-602).
        budget = settings.cross_link_request_timeout_seconds
        try:
            resp = await asyncio.wait_for(
                get_storage_client().discover_cross_links(
                    tenant_id=tenant_id,
                    fleet_id=fleet_id,
                    batch_size=batch_size,
                    threshold=threshold,
                    text_verify=text_verify,
                    target_memory_ids=target_memory_ids,
                ),
                timeout=budget,
            )
        except TimeoutError as exc:
            # Returned, not raised: the pipeline runner records a FAILED step
            # with this detail, and ``LifecycleAudit.entity_link`` puts every
            # failed step's detail into the RuntimeError the audit row and the
            # Pub/Sub nack both carry. A raise would reach the same place with
            # a bare exception repr instead of this sentence.
            return StepResult(
                StepOutcome.FAILED,
                error=exc,
                detail={
                    # Deliberately does NOT claim nothing landed. Cancelling
                    # the wait stops THIS side; it cannot roll back a
                    # transaction that may already have committed, and the
                    # client has no way to find out which happened. What is
                    # knowable is worth more to on-call than the hedge:
                    # ``entity_discover_cross_links`` folds candidate-find,
                    # the pgvector pass and the insert into ONE session, so
                    # the batch is all-or-nothing rather than half-written,
                    # and the insert is ON CONFLICT DO NOTHING on
                    # (memory_id, entity_id), so a re-run cannot double-link.
                    "error": (
                        f"cross-link discovery exceeded its {budget}s budget; the "
                        "client stopped waiting before storage answered, so this "
                        "batch may or may not have committed -- it is one "
                        "transaction and the insert is ON CONFLICT DO NOTHING, so "
                        "re-running is safe either way"
                    ),
                    "tenant_id": tenant_id,
                    "batch_size": batch_size,
                    "text_verify": text_verify,
                },
            )

        # No candidate memories → nothing to link (mirrors the source's SKIPPED).
        if resp.get("skipped"):
            return StepResult(outcome=StepOutcome.SKIPPED)

        links_created = resp.get("links_created", 0)
        ctx.data["links_created"] = links_created

        logger.info(
            "Created %d cross-links (tenant %s)",
            links_created,
            tenant_id,
        )
        return StepResult(
            outcome=StepOutcome.SUCCESS,
            detail={"links_created": links_created},
        )
