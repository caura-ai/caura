"""ResolveEntities — merge duplicate entities via embedding-based resolution."""

from __future__ import annotations

import logging
from collections import defaultdict
from uuid import UUID

from sqlalchemy import select, text

from common.models.entity import Entity
from core_api.constants import (
    ENTITY_RESOLUTION_BATCH_SIZE,
    ENTITY_RESOLUTION_CANDIDATE_LIMIT,
    ENTITY_RESOLUTION_THRESHOLD,
)
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Union-Find helpers
# ---------------------------------------------------------------------------


def _find(parent: dict[UUID, UUID], x: UUID) -> UUID:
    while parent[x] != x:
        parent[x] = parent[parent[x]]  # path compression
        x = parent[x]
    return x


def _union(parent: dict[UUID, UUID], rank: dict[UUID, int], a: UUID, b: UUID) -> None:
    ra, rb = _find(parent, a), _find(parent, b)
    if ra == rb:
        return
    if rank[ra] < rank[rb]:
        ra, rb = rb, ra
    parent[rb] = ra
    if rank[ra] == rank[rb]:
        rank[ra] += 1


class ResolveEntities:
    """Merge duplicate entities whose name embeddings exceed the similarity threshold."""

    @property
    def name(self) -> str:
        return "resolve_entities"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        tenant_id: str = ctx.data["tenant_id"]
        fleet_id: str | None = ctx.data.get("fleet_id")
        batch_size: int = ctx.data.get("entity_resolution_batch_size", ENTITY_RESOLUTION_BATCH_SIZE)

        # ── Step 1: find similar entity pairs ──────────────────────────
        threshold: float = ctx.data.get("entity_resolution_threshold", ENTITY_RESOLUTION_THRESHOLD)
        fleet_clause = ""
        params: dict = {
            "tenant_id": tenant_id,
            "threshold": threshold,
            "batch_size": batch_size,
            "candidate_limit": ENTITY_RESOLUTION_CANDIDATE_LIMIT,
        }
        if fleet_id is not None:
            fleet_clause = "AND fleet_id = :fleet_id"
            params["fleet_id"] = fleet_id

        pair_sql = text(f"""
            WITH batch AS (
                SELECT id, canonical_name, entity_type, name_embedding
                FROM entities
                WHERE tenant_id = :tenant_id
                  AND name_embedding IS NOT NULL
                  {fleet_clause}
                ORDER BY id
                LIMIT :batch_size
            )
            SELECT b.id AS id_a, nb.id AS id_b,
                   b.canonical_name AS name_a, nb.canonical_name AS name_b,
                   b.entity_type,
                   nb.sim
            FROM batch b
            JOIN LATERAL (
                SELECT e.id, e.canonical_name,
                       1 - (e.name_embedding <=> b.name_embedding) AS sim
                FROM entities e
                WHERE e.tenant_id = :tenant_id
                  AND e.name_embedding IS NOT NULL
                  AND e.id > b.id
                  AND e.entity_type = b.entity_type
                  {fleet_clause}
                  AND (1 - (e.name_embedding <=> b.name_embedding)) >= :threshold
                ORDER BY e.name_embedding <=> b.name_embedding
                LIMIT :candidate_limit
            ) nb ON true
        """)

        rows = (await ctx.require_db.execute(pair_sql, params)).all()
        if not rows:
            return StepResult(StepOutcome.SKIPPED)

        # ── Step 2: union-find clustering ──────────────────────────────
        all_ids: set[UUID] = set()
        for r in rows:
            all_ids.add(r.id_a)
            all_ids.add(r.id_b)

        parent: dict[UUID, UUID] = {uid: uid for uid in all_ids}
        rank: dict[UUID, int] = dict.fromkeys(all_ids, 0)

        for r in rows:
            _union(parent, rank, r.id_a, r.id_b)

        clusters: dict[UUID, list[UUID]] = defaultdict(list)
        for uid in all_ids:
            clusters[_find(parent, uid)].append(uid)

        # ── Process each cluster ───────────────────────────────────────
        merge_count = 0
        merged_ids: list[UUID] = []
        clusters_processed = 0
        cluster_errors = 0

        for root, cluster_ids in clusters.items():
            if len(cluster_ids) < 2:
                continue

            try:
                before = len(merged_ids)
                await self._merge_cluster(ctx, cluster_ids, merged_ids, tenant_id)
                actual_merges = len(merged_ids) - before
                merge_count += actual_merges
                if actual_merges > 0:
                    clusters_processed += 1
            except Exception:
                cluster_errors += 1
                logger.exception(
                    "Failed to merge entity cluster root=%s (%d members)",
                    root,
                    len(cluster_ids),
                )

        # ── Step 5: flush and return ───────────────────────────────────
        if clusters_processed == 0 and cluster_errors > 0:
            return StepResult(
                StepOutcome.FAILED,
                detail={
                    "error": "all clusters failed to merge",
                    "cluster_errors": cluster_errors,
                },
            )

        await ctx.require_db.flush()

        ctx.data["merge_count"] = merge_count
        ctx.data["merged_entity_ids"] = merged_ids

        return StepResult(
            StepOutcome.SUCCESS,
            detail={
                "merge_count": merge_count,
                "clusters": clusters_processed,
                "cluster_errors": cluster_errors,
                "merged_entity_ids": [str(eid) for eid in merged_ids],
            },
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _merge_cluster(
        self,
        ctx: PipelineContext,
        cluster_ids: list[UUID],
        merged_ids: list[UUID],
        tenant_id: str,
    ) -> None:
        """Pick canonical entity and merge all duplicates into it."""

        # ── Step 3: pick canonical (longest name, smallest UUID on tie) ──
        entities = (
            (
                await ctx.require_db.execute(
                    select(Entity).where(
                        Entity.id.in_(cluster_ids),
                        Entity.tenant_id == tenant_id,
                    )
                )
            )
            .scalars()
            .all()
        )

        if not entities:
            return

        canonical = max(
            entities,
            key=lambda e: (len(e.canonical_name), -e.id.int),
        )
        dupes = [e for e in entities if e.id != canonical.id]

        # ── Step 4: merge each duplicate (savepoint per dupe) ──
        for dupe in dupes:
            async with ctx.require_db.begin_nested():  # SAVEPOINT per dupe
                await self._merge_dupe_into_canonical(ctx, canonical, dupe, tenant_id)
            merged_ids.append(dupe.id)

    async def _merge_dupe_into_canonical(
        self,
        ctx: PipelineContext,
        canonical: Entity,
        dupe: Entity,
        tenant_id: str,
    ) -> None:
        """Re-point links/relations, merge aliases, delete duplicate."""
        db = ctx.require_db
        canonical_id = canonical.id
        dupe_id = dupe.id

        # 4a. Repoint MemoryEntityLink (scoped via memories.tenant_id) ──
        await db.execute(
            text("""
                DELETE FROM memory_entity_links
                WHERE entity_id = :dupe_id
                  AND memory_id IN (
                    SELECT mel.memory_id FROM memory_entity_links mel
                    JOIN memories m ON m.id = mel.memory_id
                      AND m.tenant_id = :tenant_id
                    WHERE mel.entity_id = :canonical_id
                  )
            """),
            {"dupe_id": dupe_id, "canonical_id": canonical_id, "tenant_id": tenant_id},
        )
        await db.execute(
            text("""
                UPDATE memory_entity_links
                SET entity_id = :canonical_id
                WHERE entity_id = :dupe_id
                  AND memory_id IN (
                    SELECT m.id FROM memories m
                    WHERE m.tenant_id = :tenant_id
                  )
            """),
            {"dupe_id": dupe_id, "canonical_id": canonical_id, "tenant_id": tenant_id},
        )

        # 4b. Repoint Relations (from_entity_id) ───────────────────────
        # Preserve the higher weight before deleting conflicting dupe relations.
        await db.execute(
            text("""
                UPDATE relations r_canonical
                SET weight = GREATEST(r_canonical.weight, r_dupe.weight)
                FROM relations r_dupe
                WHERE r_dupe.from_entity_id = :dupe_id
                  AND r_dupe.tenant_id = :tenant_id
                  AND r_canonical.from_entity_id = :canonical_id
                  AND r_canonical.tenant_id = :tenant_id
                  AND r_canonical.relation_type = r_dupe.relation_type
                  AND r_canonical.to_entity_id = r_dupe.to_entity_id
            """),
            {"dupe_id": dupe_id, "canonical_id": canonical_id, "tenant_id": tenant_id},
        )
        # Delete dupe's outgoing relations that would become self-loops
        # (dupe→canonical) or duplicates of canonical's existing relations.
        await db.execute(
            text("""
                DELETE FROM relations
                WHERE from_entity_id = :dupe_id
                  AND tenant_id = :tenant_id
                  AND (
                    to_entity_id = :canonical_id
                    OR (tenant_id, relation_type, to_entity_id) IN (
                        SELECT tenant_id, relation_type, to_entity_id
                        FROM relations WHERE from_entity_id = :canonical_id
                          AND tenant_id = :tenant_id
                    )
                  )
            """),
            {"dupe_id": dupe_id, "canonical_id": canonical_id, "tenant_id": tenant_id},
        )
        await db.execute(
            text("""
                UPDATE relations
                SET from_entity_id = :canonical_id
                WHERE from_entity_id = :dupe_id
                  AND tenant_id = :tenant_id
            """),
            {"dupe_id": dupe_id, "canonical_id": canonical_id, "tenant_id": tenant_id},
        )

        # 4c. Repoint Relations (to_entity_id) ─────────────────────────
        # Preserve the higher weight before deleting conflicting dupe relations.
        await db.execute(
            text("""
                UPDATE relations r_canonical
                SET weight = GREATEST(r_canonical.weight, r_dupe.weight)
                FROM relations r_dupe
                WHERE r_dupe.to_entity_id = :dupe_id
                  AND r_dupe.tenant_id = :tenant_id
                  AND r_canonical.to_entity_id = :canonical_id
                  AND r_canonical.tenant_id = :tenant_id
                  AND r_canonical.from_entity_id = r_dupe.from_entity_id
                  AND r_canonical.relation_type = r_dupe.relation_type
            """),
            {"dupe_id": dupe_id, "canonical_id": canonical_id, "tenant_id": tenant_id},
        )
        # Delete dupe's incoming relations that would become self-loops
        # (canonical→dupe) or duplicates of canonical's existing relations.
        await db.execute(
            text("""
                DELETE FROM relations
                WHERE to_entity_id = :dupe_id
                  AND tenant_id = :tenant_id
                  AND (
                    from_entity_id = :canonical_id
                    OR (tenant_id, from_entity_id, relation_type) IN (
                        SELECT tenant_id, from_entity_id, relation_type
                        FROM relations WHERE to_entity_id = :canonical_id
                          AND tenant_id = :tenant_id
                    )
                  )
            """),
            {"dupe_id": dupe_id, "canonical_id": canonical_id, "tenant_id": tenant_id},
        )
        await db.execute(
            text("""
                UPDATE relations
                SET to_entity_id = :canonical_id
                WHERE to_entity_id = :dupe_id
                  AND tenant_id = :tenant_id
            """),
            {"dupe_id": dupe_id, "canonical_id": canonical_id, "tenant_id": tenant_id},
        )

        # 4d. Merge aliases ─────────────────────────────────────────────
        canonical_attrs = dict(canonical.attributes or {})
        dupe_attrs = dict(dupe.attributes or {})
        aliases: set[str] = set(canonical_attrs.get("_aliases", []))
        aliases.add(canonical.canonical_name)
        aliases.add(dupe.canonical_name)
        aliases.update(dupe_attrs.get("_aliases", []))
        canonical_attrs["_aliases"] = sorted(aliases)  # sorted for determinism
        canonical.attributes = canonical_attrs

        # 4e. Delete duplicate entity ──────────────────────────────────
        await db.delete(dupe)
