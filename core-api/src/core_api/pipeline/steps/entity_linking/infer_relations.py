"""InferRelations — create co-occurrence-based relations between entities."""

from __future__ import annotations

import logging

from sqlalchemy import text

from core_api.constants import (
    MAX_RELATION_WEIGHT,
    MIN_COOCCURRENCE_FOR_RELATION,
    RELATION_INFERENCE_BATCH_SIZE,
    RELATION_REINFORCE_DELTA,
)
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult

logger = logging.getLogger(__name__)


class InferRelations:
    """Infer 'related_to' relations from entity co-occurrence in shared memories."""

    @property
    def name(self) -> str:
        return "infer_relations"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        """Scan co-occurring entity pairs and create or reinforce relations."""
        tenant_id: str = ctx.data["tenant_id"]
        fleet_id: str | None = ctx.data.get("fleet_id")
        batch_size: int = ctx.data.get("relation_inference_batch_size", RELATION_INFERENCE_BATCH_SIZE)
        min_cooccurrence: int = ctx.data.get("min_cooccurrence", MIN_COOCCURRENCE_FOR_RELATION)

        # ── 1. Co-occurrence query ────────────────────────────────────
        entity_fleet_clause = "AND fleet_id = :fleet_id" if fleet_id else ""
        memory_fleet_clause = "AND mem.fleet_id = :fleet_id" if fleet_id else ""
        cooccurrences = (
            await ctx.require_db.execute(
                text(f"""
                    WITH tenant_entity_ids AS (
                        SELECT id FROM entities
                        WHERE tenant_id = :tenant_id
                          {entity_fleet_clause}
                    )
                    SELECT a.entity_id AS from_id, b.entity_id AS to_id,
                           COUNT(*) AS cooccur
                    FROM memory_entity_links a
                    JOIN memory_entity_links b
                      ON a.memory_id = b.memory_id
                      AND a.entity_id < b.entity_id
                    JOIN memories mem
                      ON mem.id = a.memory_id
                      AND mem.tenant_id = :tenant_id
                      AND mem.deleted_at IS NULL
                      {memory_fleet_clause}
                    WHERE a.entity_id IN (SELECT id FROM tenant_entity_ids)
                      AND b.entity_id IN (SELECT id FROM tenant_entity_ids)
                    GROUP BY a.entity_id, b.entity_id
                    HAVING COUNT(*) >= :min_cooccurrence
                    ORDER BY cooccur DESC
                    LIMIT :batch_size
                """),
                {
                    "tenant_id": tenant_id,
                    **({"fleet_id": fleet_id} if fleet_id else {}),
                    "min_cooccurrence": min_cooccurrence,
                    "batch_size": batch_size,
                },
            )
        ).all()

        if not cooccurrences:
            return StepResult(outcome=StepOutcome.SKIPPED)

        # ── 2. Bulk-fetch existing 'related_to' relations for all pairs ─
        # Scoped by tenant_id only (not fleet_id) so fleet-scoped runs can
        # reinforce relations created by full runs; the unique constraint
        # uq_relations_natural_key does not include fleet_id.
        all_entity_ids = list({eid for row in cooccurrences for eid in (row[0], row[1])})
        existing_rows = (
            await ctx.require_db.execute(
                text("""
                    SELECT from_entity_id, to_entity_id, id, weight
                    FROM relations
                    WHERE tenant_id = :tenant_id
                      AND relation_type = 'related_to'
                      AND (from_entity_id = ANY(CAST(:ids AS uuid[])) OR to_entity_id = ANY(CAST(:ids AS uuid[])))
                """),
                {
                    "tenant_id": tenant_id,
                    "ids": all_entity_ids,
                },
            )
        ).all()

        # Build lookup: frozenset({from_id, to_id}) -> (rel_id, weight)
        existing_map: dict[frozenset, tuple] = {frozenset({r[0], r[1]}): (r[2], r[3]) for r in existing_rows}

        # ── 3. Split into reinforce vs. create batches ────────────────
        reinforce_batch: list[dict] = []
        insert_batch: list[dict] = []

        for from_id, to_id, cooccur in cooccurrences:
            pair_key = frozenset({from_id, to_id})
            existing = existing_map.get(pair_key)

            if existing:
                rel_id, current_weight = existing
                new_weight = min(
                    current_weight + cooccur * RELATION_REINFORCE_DELTA,
                    MAX_RELATION_WEIGHT,
                )
                reinforce_batch.append(
                    {
                        "rel_id": rel_id,
                        # Already clamped to MAX_RELATION_WEIGHT above — bound
                        # directly below (no SQL-side LEAST), matching the
                        # INSERT path's ``:weight``.
                        "new_weight": new_weight,
                        "tenant_id": tenant_id,
                    }
                )
            else:
                weight = min(cooccur * RELATION_REINFORCE_DELTA, MAX_RELATION_WEIGHT)
                insert_batch.append(
                    {
                        "tenant_id": tenant_id,
                        "fleet_id": fleet_id,
                        "from_id": from_id,
                        "to_id": to_id,
                        "weight": weight,
                    }
                )

        # ── 4. Execute batched UPDATEs ────────────────────────────────
        relations_reinforced = 0
        if reinforce_batch:
            await ctx.require_db.execute(
                # ``SET weight = :new_weight`` NOT ``LEAST(:new_weight, :max_weight)``:
                # Postgres resolves ``LEAST`` over two untyped bind params as
                # ``text`` and then rejects the assignment to the
                # double-precision ``weight`` column (asyncpg
                # DatatypeMismatchError, prod 2026-06-13). Direct assignment
                # infers the column type from context; new_weight is already
                # clamped in Python.
                text("""
                    UPDATE relations
                    SET weight = :new_weight
                    WHERE id = :rel_id AND tenant_id = :tenant_id
                """),
                reinforce_batch,
            )
            relations_reinforced = len(reinforce_batch)

        # ── 5. Execute batched INSERTs ────────────────────────────────
        relations_created = 0
        if insert_batch:
            result = await ctx.require_db.execute(
                text("""
                    INSERT INTO relations
                        (tenant_id, fleet_id, from_entity_id, relation_type,
                         to_entity_id, weight)
                    VALUES
                        (:tenant_id, :fleet_id, :from_id, 'related_to',
                         :to_id, :weight)
                    ON CONFLICT ON CONSTRAINT uq_relations_natural_key
                    DO NOTHING
                """),
                insert_batch,
            )
            relations_created = result.rowcount if result.rowcount >= 0 else len(insert_batch)

        await ctx.require_db.flush()

        ctx.data["relations_created"] = relations_created
        ctx.data["relations_reinforced"] = relations_reinforced

        logger.info(
            "Inferred relations for tenant %s: created=%d reinforced=%d",
            tenant_id,
            relations_created,
            relations_reinforced,
        )
        return StepResult(
            outcome=StepOutcome.SUCCESS,
            detail={
                "relations_created": relations_created,
                "relations_reinforced": relations_reinforced,
            },
        )
