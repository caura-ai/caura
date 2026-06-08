"""DiscoverCrossLinks — link under-connected memories to similar entities."""

from __future__ import annotations

import logging

from sqlalchemy import text

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

        # ── 1. Find candidate memories ──────────────────────────────
        fleet_clause = "AND m.fleet_id = :fleet_id" if fleet_id else ""

        if target_memory_ids:
            # Targeted mode: specific memories (e.g. after entity extraction)
            candidates = (
                await ctx.require_db.execute(
                    text(f"""
                        SELECT m.id, m.content, m.embedding
                        FROM memories m
                        WHERE m.id = ANY(CAST(:memory_ids AS uuid[]))
                          AND m.tenant_id = :tenant_id
                          AND m.deleted_at IS NULL
                          AND m.status = 'active'
                          AND m.embedding IS NOT NULL
                          {fleet_clause}
                    """),
                    {
                        "tenant_id": tenant_id,
                        "memory_ids": [str(mid) for mid in target_memory_ids],
                        **({"fleet_id": fleet_id} if fleet_id else {}),
                    },
                )
            ).all()
        else:
            # Batch mode: under-connected memories (lifecycle / scheduled)
            candidates = (
                await ctx.require_db.execute(
                    text(f"""
                        SELECT m.id, m.content, m.embedding
                        FROM memories m
                        LEFT JOIN memory_entity_links mel ON mel.memory_id = m.id
                        WHERE m.tenant_id = :tenant_id
                          AND m.deleted_at IS NULL
                          AND m.status = 'active'
                          AND m.embedding IS NOT NULL
                          {fleet_clause}
                        GROUP BY m.id
                        HAVING COUNT(mel.entity_id) < 3
                        ORDER BY m.created_at DESC
                        LIMIT :batch_size
                    """),
                    {
                        "tenant_id": tenant_id,
                        **({"fleet_id": fleet_id} if fleet_id else {}),
                        "batch_size": batch_size,
                    },
                )
            ).all()

        if not candidates:
            return StepResult(outcome=StepOutcome.SKIPPED)

        # ── 2. Find similar entities for all candidate memories (LATERAL JOIN) ──
        entity_fleet_clause = "AND e.fleet_id = :fleet_id" if fleet_id else ""
        memory_ids = [row[0] for row in candidates]
        content_map = {row[0]: row[1] for row in candidates}

        lateral_query = text(f"""
            SELECT m.id AS memory_id,
                   e.id AS entity_id, e.canonical_name, e.attributes, e.sim
            FROM (SELECT id, embedding FROM memories
                  WHERE id = ANY(CAST(:memory_ids AS uuid[])) AND tenant_id = :tenant_id) m
            JOIN LATERAL (
                SELECT e.id, e.canonical_name, e.attributes,
                       1 - (e.name_embedding <=> m.embedding) AS sim
                FROM entities e
                WHERE e.tenant_id = :tenant_id
                  AND e.name_embedding IS NOT NULL
                  AND (1 - (e.name_embedding <=> m.embedding)) >= :threshold
                  {entity_fleet_clause}
                ORDER BY e.name_embedding <=> m.embedding
                LIMIT 10
            ) e ON true
            ORDER BY m.id, e.sim DESC
        """)

        lateral_rows = (
            await ctx.require_db.execute(
                lateral_query,
                {
                    "tenant_id": tenant_id,
                    "memory_ids": memory_ids,
                    "threshold": threshold,
                    **({"fleet_id": fleet_id} if fleet_id else {}),
                },
            )
        ).all()

        # Filter candidates in Python, then bulk-insert
        to_insert: list[dict] = []
        for memory_id, entity_id, canonical_name, attributes, _sim in lateral_rows:
            if text_verify:
                content = content_map.get(memory_id, "")
                names_to_check = [canonical_name]
                if attributes and isinstance(attributes, dict):
                    names_to_check.extend(attributes.get("_aliases", []))
                content_lower = content.lower() if content else ""
                if not any(n.lower() in content_lower for n in names_to_check):
                    continue
            to_insert.append({"memory_id": memory_id, "entity_id": entity_id})

        links_created = 0
        if to_insert:
            try:
                # memory_entity_links has a composite PK (memory_id, entity_id)
                # and no surrogate `id` column, so RETURNING must reference real
                # columns. With ON CONFLICT DO NOTHING, only actually-inserted
                # rows are returned, so the count below stays accurate.
                insert_link_returning = text("""
                    INSERT INTO memory_entity_links (memory_id, entity_id, role)
                    VALUES (:memory_id, :entity_id, 'mentioned')
                    ON CONFLICT (memory_id, entity_id) DO NOTHING
                    RETURNING memory_id, entity_id
                """)
                result = await ctx.require_db.execute(insert_link_returning, to_insert)
                links_created = len(result.all())
            except Exception:
                logger.exception(
                    "Error bulk-inserting %d cross-links for tenant %s",
                    len(to_insert),
                    tenant_id,
                )
                return StepResult(
                    outcome=StepOutcome.FAILED,
                    detail={"error": "bulk insert failed", "attempted": len(to_insert)},
                )

        await ctx.require_db.flush()
        ctx.data["links_created"] = links_created

        logger.info(
            "Created %d cross-links for %d candidate memories (tenant %s)",
            links_created,
            len(candidates),
            tenant_id,
        )
        return StepResult(
            outcome=StepOutcome.SUCCESS,
            detail={"links_created": links_created},
        )
