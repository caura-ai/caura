"""Repository for memories table queries.

All SQLAlchemy queries touching the memories table live here.
Services and pipeline steps must use this repository instead
of executing SQL directly.  The repository never calls db.commit().
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import and_, case, func, literal_column, or_, select, text
from sqlalchemy import update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession

from common.models.entity import MemoryEntityLink
from common.models.memory import Memory
from core_api.constants import (
    CONTRADICTION_CANDIDATE_MAX,
    CONTRADICTION_SIMILARITY_THRESHOLD,
    RECALL_BOOST_SCALE,
    SEMANTIC_DEDUP_CANDIDATE_LIMIT,
    SEMANTIC_DEDUP_THRESHOLD,
    TYPE_DECAY_DAYS,
)
from core_api.pagination import paginated_order_by
from core_api.repositories import scope_sql
from core_api.schemas import EntityLinkOut


class MemoryRepository:
    """Single point of DB access for Memory rows."""

    # ------------------------------------------------------------------
    # A) Core CRUD
    # ------------------------------------------------------------------

    async def get_by_id(self, db: AsyncSession, memory_id: UUID) -> Memory | None:
        return await db.get(Memory, memory_id)

    async def get_by_id_for_tenant(
        self,
        db: AsyncSession,
        memory_id: UUID,
        tenant_id: str,
    ) -> Memory | None:
        memory = await db.get(Memory, memory_id)
        if memory is None or memory.tenant_id != tenant_id or memory.deleted_at is not None:
            return None
        return memory

    async def add(self, db: AsyncSession, memory: Memory) -> None:
        db.add(memory)
        await db.flush()

    async def add_all(self, db: AsyncSession, memories: list[Memory]) -> None:
        db.add_all(memories)
        await db.flush()

    async def soft_delete(self, db: AsyncSession, memory: Memory) -> None:
        memory.deleted_at = datetime.now(UTC)
        memory.status = "deleted"

    async def update_status(self, db: AsyncSession, memory_id: UUID, status: str) -> None:
        await db.execute(sql_update(Memory).where(Memory.id == memory_id).values(status=status))

    async def update_embedding(
        self,
        db: AsyncSession,
        memory_id: UUID,
        embedding: list[float],
        metadata: dict | None = None,
    ) -> None:
        values: dict = {"embedding": embedding}
        if metadata is not None:
            values["metadata_"] = metadata
        await db.execute(sql_update(Memory).where(Memory.id == memory_id).values(**values))

    # ------------------------------------------------------------------
    # A2) Filtered list (non-semantic browse with visibility scoping)
    # ------------------------------------------------------------------

    async def list_by_filters(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        caller_agent_id: str | None = None,
        fleet_id: str | None = None,
        written_by: str | None = None,
        memory_type: str | None = None,
        status: str | None = None,
        run_id: str | None = None,
        weight_min: float | None = None,
        weight_max: float | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        include_deleted: bool = False,
        sort: str = "created_at",
        order: str = "desc",
        limit: int = 25,
        offset: int = 0,
        cursor_ts: datetime | None = None,
        cursor_id: UUID | None = None,
        readable_tenant_ids: list[str] | None = None,
    ) -> list[Memory]:
        """Filter, sort, paginate memories with proper visibility scoping.

        Returns ``limit + 1`` rows so the caller can detect ``has_more``.

        **Visibility semantics** (mirrors ``scored_search``):
        - ``scope_org``: visible to everyone in the tenant.
        - ``scope_team``: visible to everyone in the tenant.
        - ``scope_agent``: visible ONLY to the authoring agent.
          If ``caller_agent_id`` is known, show the caller's own
          ``scope_agent`` memories + all team/org. If unknown, exclude
          all ``scope_agent`` rows.

        **Cross-tenant widening:** when ``readable_tenant_ids`` is a
        non-empty list, the query expands from ``tenant_id = $1`` to
        ``tenant_id = ANY($1)``. The ``tenant_id`` arg is still used
        as the binding/home tenant (for visibility scoping signals);
        the list controls which tenants' rows are eligible. Mirrors
        ``scored_search``'s widening contract (#154).
        """
        from sqlalchemy import tuple_

        if readable_tenant_ids:
            stmt = select(Memory).where(Memory.tenant_id.in_(readable_tenant_ids))
        else:
            stmt = select(Memory).where(Memory.tenant_id == tenant_id)

        # ── Visibility predicate (critical: prevents scope_agent leaks) ──
        if caller_agent_id:
            stmt = stmt.where(
                or_(
                    Memory.visibility == "scope_org",
                    Memory.visibility == "scope_team",
                    and_(
                        Memory.visibility == "scope_agent",
                        Memory.agent_id == caller_agent_id,
                    ),
                )
            )
        else:
            stmt = stmt.where(Memory.visibility != "scope_agent")

        # ── Standard filters ──
        if fleet_id:
            stmt = stmt.where(Memory.fleet_id == fleet_id)
        if written_by:
            stmt = stmt.where(Memory.agent_id == written_by)
        if memory_type:
            stmt = stmt.where(Memory.memory_type == memory_type)
        if status:
            stmt = stmt.where(Memory.status == status)
        if run_id is not None:
            # Indexed by ``ix_memories_run_id (tenant_id, run_id)`` — drives
            # the "show me memories from this ingest batch" filter used by
            # the dashboard Past Uploads tab.
            stmt = stmt.where(Memory.run_id == run_id)
        if weight_min is not None:
            stmt = stmt.where(Memory.weight >= weight_min)
        if weight_max is not None:
            stmt = stmt.where(Memory.weight <= weight_max)
        if created_after is not None:
            stmt = stmt.where(Memory.created_at >= created_after)
        if created_before is not None:
            stmt = stmt.where(Memory.created_at <= created_before)
        if not include_deleted:
            stmt = stmt.where(Memory.deleted_at.is_(None))

        # ── Cursor pagination (created_at desc only) ──
        if cursor_ts is not None and cursor_id is not None:
            stmt = stmt.where(tuple_(Memory.created_at, Memory.id) < tuple_(cursor_ts, cursor_id))

        # ── Sort + offset + limit ──
        col = getattr(Memory, sort)
        stmt = stmt.order_by(*paginated_order_by(col, Memory.id, order))
        if offset and not cursor_ts:
            stmt = stmt.offset(offset)
        stmt = stmt.limit(limit + 1)

        rows = (await db.execute(stmt)).scalars().all()
        return list(rows)

    # ------------------------------------------------------------------
    # B) Content hash / dedup
    # ------------------------------------------------------------------

    async def find_by_content_hash(
        self,
        db: AsyncSession,
        tenant_id: str,
        content_hash: str,
        fleet_id: str | None = None,
    ) -> Memory | None:
        stmt = select(Memory).where(
            Memory.tenant_id == tenant_id,
            Memory.content_hash == content_hash,
            Memory.deleted_at.is_(None),
        )
        if fleet_id:
            stmt = stmt.where(Memory.fleet_id == fleet_id)
        else:
            stmt = stmt.where(Memory.fleet_id.is_(None))
        return (await db.execute(stmt)).scalar_one_or_none()

    async def find_duplicate_hash(
        self,
        db: AsyncSession,
        tenant_id: str,
        content_hash: str,
        fleet_id: str | None = None,
        exclude_id: UUID | None = None,
    ) -> UUID | None:
        stmt = select(Memory.id).where(
            Memory.tenant_id == tenant_id,
            Memory.content_hash == content_hash,
            Memory.deleted_at.is_(None),
        )
        if fleet_id:
            stmt = stmt.where(Memory.fleet_id == fleet_id)
        else:
            stmt = stmt.where(Memory.fleet_id.is_(None))
        if exclude_id is not None:
            stmt = stmt.where(Memory.id != exclude_id)
        return (await db.execute(stmt)).scalar_one_or_none()

    async def find_embedding_by_content_hash(
        self,
        db: AsyncSession,
        tenant_id: str,
        content_hash: str,
    ) -> list[float] | None:
        stmt = (
            select(Memory.embedding)
            .where(
                Memory.tenant_id == tenant_id,
                Memory.content_hash == content_hash,
                Memory.embedding.isnot(None),
                Memory.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    async def find_semantic_duplicate(
        self,
        db: AsyncSession,
        tenant_id: str,
        fleet_id: str | None,
        embedding: list[float],
        exclude_id: UUID | None = None,
        visibility: str | None = None,
    ) -> Memory | None:
        distance = Memory.embedding.cosine_distance(embedding)
        similarity = (1.0 - distance).label("similarity")

        stmt = (
            select(Memory, similarity)
            .where(
                Memory.tenant_id == tenant_id,
                Memory.deleted_at.is_(None),
                Memory.status.in_(("active", "confirmed", "pending")),
                Memory.embedding.is_not(None),
            )
            .where((1.0 - distance) >= SEMANTIC_DEDUP_THRESHOLD)
            .order_by(distance)
            .limit(SEMANTIC_DEDUP_CANDIDATE_LIMIT)
        )

        if fleet_id:
            stmt = stmt.where(Memory.fleet_id == fleet_id)
        else:
            stmt = stmt.where(Memory.fleet_id.is_(None))
        if visibility:
            stmt = stmt.where(Memory.visibility == visibility)
        if exclude_id is not None:
            stmt = stmt.where(Memory.id != exclude_id)

        result = await db.execute(stmt)
        row = result.first()
        return row.Memory if row else None

    async def bulk_find_by_content_hashes(
        self,
        db: AsyncSession,
        tenant_id: str,
        hashes: list[str],
        fleet_id: str | None = None,
    ) -> dict[str, UUID]:
        stmt = select(Memory.content_hash, Memory.id).where(
            Memory.tenant_id == tenant_id,
            Memory.content_hash.in_(hashes),
            Memory.deleted_at.is_(None),
        )
        if fleet_id:
            stmt = stmt.where(Memory.fleet_id == fleet_id)
        else:
            stmt = stmt.where(Memory.fleet_id.is_(None))
        rows = (await db.execute(stmt)).all()
        return {row[0]: row[1] for row in rows}

    # ------------------------------------------------------------------
    # C) Scored search (CTE-based)
    # ------------------------------------------------------------------

    async def scored_search(
        self,
        db: AsyncSession,
        tenant_id: str,
        embedding: list[float],
        query: str,
        *,
        fleet_ids: list[str] | None = None,
        caller_agent_id: str | None = None,
        filter_agent_id: str | None = None,
        memory_type_filter: str | None = None,
        status_filter: str | None = None,
        valid_at=None,
        boosted_memory_ids: set[UUID] | None = None,
        memory_boost_factor: dict[UUID, float] | None = None,
        search_params: dict,
        temporal_window: timedelta | None = None,
        recall_boost_enabled: bool = True,
        top_k: int = 10,
    ) -> list[SimpleNamespace]:
        """Execute the full CTE-based scored search with entity-link JOIN.

        Returns a list of SimpleNamespace objects with attributes:
        Memory, score, similarity, vec_sim, entity_links.
        """
        boosted_memory_ids = boosted_memory_ids or set()
        memory_boost_factor = memory_boost_factor or {}
        sp = search_params

        _fts_weight = sp["fts_weight"]
        _freshness_floor = sp["freshness_floor"]
        _freshness_decay_days = sp["freshness_decay_days"]
        _recall_boost_cap = sp["recall_boost_cap"]
        _recall_decay_window_days = sp["recall_decay_window_days"]
        _similarity_blend = sp["similarity_blend"]
        _top_k = sp.get("top_k", top_k)

        # ── Scoring expressions ──
        distance = Memory.embedding.cosine_distance(embedding)
        vec_sim = (1.0 - distance).label("vec_sim")

        ts_query = func.plainto_tsquery("english", query)
        raw_fts = func.ts_rank_cd(Memory.search_vector, ts_query)
        fts_score = (raw_fts / (1.0 + raw_fts)).label("fts_score")

        similarity = ((1.0 - _fts_weight) * vec_sim + _fts_weight * fts_score).label("similarity")

        anchor = func.greatest(
            Memory.created_at,
            func.coalesce(Memory.ts_valid_start, Memory.created_at),
        )
        age_days = func.extract("epoch", func.now() - anchor) / 86400.0

        type_decay = case(
            *[(Memory.memory_type == mt, float(days)) for mt, days in TYPE_DECAY_DAYS.items()],
            else_=float(_freshness_decay_days),
        ).label("type_decay_days")

        freshness = case(
            (
                and_(
                    Memory.ts_valid_end.is_not(None),
                    Memory.ts_valid_end < func.now(),
                ),
                _freshness_floor,
            ),
            (Memory.ts_valid_end.is_not(None), 1.0),
            (
                age_days < type_decay,
                1.0 - (age_days / type_decay) * (1.0 - _freshness_floor),
            ),
            else_=_freshness_floor,
        ).label("freshness")

        if recall_boost_enabled:
            days_since_recall = (
                func.extract(
                    "epoch",
                    func.now() - func.coalesce(Memory.last_recalled_at, Memory.created_at),
                )
                / 86400.0
            )
            recency_factor = func.greatest(0.0, 1.0 - days_since_recall / _recall_decay_window_days)
            recall_boost_expr = (
                1.0
                + (_recall_boost_cap - 1.0)
                * recency_factor
                * Memory.recall_count
                / (Memory.recall_count + RECALL_BOOST_SCALE)
            ).label("recall_boost")
        else:
            recall_boost_expr = literal_column("1.0").label("recall_boost")

        base_score = (_similarity_blend * similarity + (1.0 - _similarity_blend) * Memory.weight).label(
            "base_score"
        )

        if temporal_window is not None:
            cutoff = func.now() - temporal_window
            temporal_boost = case(
                (Memory.created_at >= cutoff, 1.3),
                else_=1.0,
            ).label("temporal_boost")
        else:
            temporal_boost = literal_column("1.0").label("temporal_boost")

        if boosted_memory_ids:
            boost_tiers: dict[float, list[UUID]] = {}
            for mid, factor in memory_boost_factor.items():
                boost_tiers.setdefault(factor, []).append(mid)
            whens = [
                (Memory.id.in_(mids), factor) for factor, mids in sorted(boost_tiers.items(), reverse=True)
            ]
            entity_boost = case(*whens, else_=1.0).label("entity_boost")
            score = (base_score * freshness * entity_boost * recall_boost_expr * temporal_boost).label(
                "score"
            )
        else:
            score = (base_score * freshness * recall_boost_expr * temporal_boost).label("score")

        # ── Build scored CTE ──
        scored_stmt = (
            select(
                Memory.id.label("mem_id"),
                score,
                similarity,
                vec_sim,
            )
            .where(Memory.tenant_id == tenant_id)
            .where(Memory.embedding.is_not(None))
            .where(Memory.deleted_at.is_(None))
        )

        if fleet_ids:
            scored_stmt = scored_stmt.where(
                or_(
                    Memory.fleet_id.in_(fleet_ids),
                    Memory.fleet_id.is_(None),
                    Memory.visibility == "scope_org",
                )
            )

        if caller_agent_id:
            visibility_filter = or_(
                Memory.visibility == "scope_org",
                Memory.visibility == "scope_team",
                and_(
                    Memory.visibility == "scope_agent",
                    Memory.agent_id == caller_agent_id,
                ),
            )
            scored_stmt = scored_stmt.where(visibility_filter)
        else:
            scored_stmt = scored_stmt.where(Memory.visibility != "scope_agent")

        if filter_agent_id:
            scored_stmt = scored_stmt.where(Memory.agent_id == filter_agent_id)
        if memory_type_filter:
            scored_stmt = scored_stmt.where(Memory.memory_type == memory_type_filter)
        if status_filter:
            scored_stmt = scored_stmt.where(Memory.status == status_filter)
        if valid_at:
            scored_stmt = scored_stmt.where(
                or_(
                    Memory.ts_valid_start.is_(None),
                    Memory.ts_valid_start <= valid_at,
                ),
            ).where(
                or_(
                    Memory.ts_valid_end.is_(None),
                    Memory.ts_valid_end >= valid_at,
                ),
            )

        scored_stmt = scored_stmt.order_by(score.desc(), Memory.created_at.desc()).limit(_top_k)

        scored_cte = scored_stmt.cte("scored")

        # ── Outer query: JOIN Memory + LEFT JOIN entity links ──
        stmt = (
            select(
                Memory,
                scored_cte.c.score,
                scored_cte.c.similarity,
                scored_cte.c.vec_sim,
                MemoryEntityLink.entity_id,
                MemoryEntityLink.role,
            )
            .join(scored_cte, Memory.id == scored_cte.c.mem_id)
            .outerjoin(MemoryEntityLink, Memory.id == MemoryEntityLink.memory_id)
            .order_by(scored_cte.c.score.desc(), Memory.created_at.desc())
        )

        result = await db.execute(stmt)

        # ── Group rows by memory, collecting entity links ──
        grouped: OrderedDict[UUID, SimpleNamespace] = OrderedDict()
        for row in result.all():
            mid = row.Memory.id
            if mid not in grouped:
                grouped[mid] = SimpleNamespace(
                    Memory=row.Memory,
                    score=row.score,
                    similarity=row.similarity,
                    vec_sim=row.vec_sim,
                    entity_links=[],
                )
            if row.entity_id is not None:
                grouped[mid].entity_links.append(EntityLinkOut(entity_id=row.entity_id, role=row.role))

        return list(grouped.values())

    # ------------------------------------------------------------------
    # D) Contradiction detection
    # ------------------------------------------------------------------

    async def find_rdf_conflicts(
        self,
        db: AsyncSession,
        memory: Memory,
    ) -> list[Memory]:
        stmt = select(Memory).where(
            Memory.tenant_id == memory.tenant_id,
            Memory.deleted_at.is_(None),
            Memory.status.in_(("active", "confirmed", "pending")),
            Memory.subject_entity_id == memory.subject_entity_id,
            func.lower(Memory.predicate) == memory.predicate.lower(),
            Memory.object_value != memory.object_value,
            Memory.id != memory.id,
        )
        if memory.fleet_id:
            stmt = stmt.where(Memory.fleet_id == memory.fleet_id)
        else:
            stmt = stmt.where(Memory.fleet_id.is_(None))

        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def find_similar_candidates(
        self,
        db: AsyncSession,
        memory: Memory,
        embedding: list[float],
        threshold: float = CONTRADICTION_SIMILARITY_THRESHOLD,
        limit: int = CONTRADICTION_CANDIDATE_MAX,
    ) -> list[Memory]:
        distance = Memory.embedding.cosine_distance(embedding)
        similarity = (1.0 - distance).label("similarity")

        stmt = (
            select(Memory, similarity)
            .where(
                Memory.tenant_id == memory.tenant_id,
                Memory.deleted_at.is_(None),
                Memory.status.in_(("active", "confirmed", "pending")),
                Memory.embedding.is_not(None),
                Memory.id != memory.id,
            )
            .where((1.0 - distance) >= threshold)
            .order_by(distance)
            .limit(limit)
        )

        if memory.fleet_id:
            stmt = stmt.where(Memory.fleet_id == memory.fleet_id)
        else:
            stmt = stmt.where(Memory.fleet_id.is_(None))

        visibility = memory.visibility or "scope_team"
        stmt = stmt.where(Memory.visibility == visibility)

        result = await db.execute(stmt)
        return [row.Memory for row in result.all()]

    # ------------------------------------------------------------------
    # E) Lifecycle batch
    # ------------------------------------------------------------------

    async def archive_expired(
        self,
        db: AsyncSession,
        tenant_id: str,
        fleet_id: str | None = None,
        batch_size: int = 500,
    ) -> int:
        params: dict = {"tenant_id": tenant_id, "batch_size": batch_size}
        fleet_clause = ""
        if fleet_id:
            fleet_clause = "AND fleet_id = :fleet_id"
            params["fleet_id"] = fleet_id

        result = await db.execute(
            text(f"""
            UPDATE memories SET status = 'outdated'
            WHERE id IN (
                SELECT id FROM memories
                WHERE tenant_id = :tenant_id
                  {fleet_clause}
                  AND ts_valid_end < NOW()
                  AND status = 'active'
                  AND deleted_at IS NULL
                LIMIT :batch_size
            )
            RETURNING id
        """),
            params,
        )
        return len(result.all())

    async def archive_stale(
        self,
        db: AsyncSession,
        tenant_id: str,
        fleet_id: str | None = None,
        stale_days: int = 90,
        max_weight: float = 0.3,
        batch_size: int = 500,
    ) -> int:
        params: dict = {
            "tenant_id": tenant_id,
            "stale_days": stale_days,
            "max_weight": max_weight,
            "batch_size": batch_size,
        }
        fleet_clause = ""
        if fleet_id:
            fleet_clause = "AND fleet_id = :fleet_id"
            params["fleet_id"] = fleet_id

        result = await db.execute(
            text(f"""
            UPDATE memories SET status = 'archived'
            WHERE id IN (
                SELECT id FROM memories
                WHERE tenant_id = :tenant_id
                  {fleet_clause}
                  AND created_at < NOW() - INTERVAL '1 day' * :stale_days
                  AND recall_count = 0
                  AND weight < :max_weight
                  AND status = 'active'
                  AND deleted_at IS NULL
                LIMIT :batch_size
            )
            RETURNING id
        """),
            params,
        )
        return len(result.all())

    async def count_active(
        self,
        db: AsyncSession,
        tenant_id: str,
        fleet_id: str | None = None,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(Memory)
            .where(
                Memory.tenant_id == tenant_id,
                Memory.status == "active",
                Memory.deleted_at.is_(None),
            )
        )
        if fleet_id:
            stmt = stmt.where(Memory.fleet_id == fleet_id)
        result = await db.execute(stmt)
        return result.scalar() or 0

    async def count_all(self, db: AsyncSession) -> int:
        result = await db.scalar(select(func.count()).select_from(Memory))
        return result or 0

    # ------------------------------------------------------------------
    # F) Crystallizer hygiene
    # ------------------------------------------------------------------

    async def find_missing_embeddings(
        self,
        db: AsyncSession,
        tenant_id: str,
        fleet_id: str | None,
        batch_size: int = 100,
    ) -> list[tuple]:
        scope, params = scope_sql(tenant_id, fleet_id)
        result = await db.execute(
            text(f"""
            SELECT m.id, m.content
            FROM memories m
            WHERE {scope}
              AND m.embedding IS NULL
              AND m.deleted_at IS NULL
            LIMIT :batch_size
        """),
            {**params, "batch_size": batch_size},
        )
        return result.all()

    async def find_near_duplicate_candidates(
        self,
        db: AsyncSession,
        tenant_id: str,
        fleet_id: str | None,
        batch_size: int,
        offset: int = 0,
    ) -> list[tuple]:
        scope, params = scope_sql(tenant_id, fleet_id)
        result = await db.execute(
            text(f"""
            SELECT m.id, m.embedding
            FROM memories m
            WHERE {scope}
              AND m.embedding IS NOT NULL
              AND m.deleted_at IS NULL
              AND m.last_dedup_checked_at IS NULL
            ORDER BY m.created_at DESC
            LIMIT :batch_size OFFSET :batch_offset
        """),
            {**params, "batch_size": batch_size, "batch_offset": offset},
        )
        return result.all()

    async def find_neighbors_by_embedding(
        self,
        db: AsyncSession,
        tenant_id: str,
        fleet_id: str | None,
        query_embedding,
        exclude_id: UUID,
        threshold: float,
        limit: int,
    ) -> list[tuple]:
        scope, params = scope_sql(tenant_id, fleet_id, table="n")
        result = await db.execute(
            text(f"""
            SELECT n.id,
                   1 - (n.embedding <=> :query_emb) AS similarity
            FROM memories n
            WHERE {scope}
              AND n.embedding IS NOT NULL
              AND n.deleted_at IS NULL
              AND n.id != :self_id
              AND 1 - (n.embedding <=> :query_emb) >= :threshold
            ORDER BY n.embedding <=> :query_emb
            LIMIT :k
        """),
            {
                **params,
                "query_emb": str(query_embedding),
                "self_id": exclude_id,
                "threshold": threshold,
                "k": limit,
            },
        )
        return result.all()

    async def mark_dedup_checked(
        self,
        db: AsyncSession,
        memory_ids: list[UUID],
    ) -> None:
        if not memory_ids:
            return
        await db.execute(
            sql_update(Memory).where(Memory.id.in_(memory_ids)).values(last_dedup_checked_at=func.now())
        )

    async def find_expired_still_active(
        self,
        db: AsyncSession,
        tenant_id: str,
        fleet_id: str | None,
    ) -> list[tuple]:
        scope, params = scope_sql(tenant_id, fleet_id)
        result = await db.execute(
            text(f"""
            SELECT m.id
            FROM memories m
            WHERE {scope}
              AND m.ts_valid_end < NOW()
              AND m.status = 'active'
              AND m.deleted_at IS NULL
            LIMIT 100
        """),
            params,
        )
        return result.all()

    async def find_stale_count(
        self,
        db: AsyncSession,
        tenant_id: str,
        fleet_id: str | None,
        stale_days: int,
        max_weight: float,
    ) -> list[tuple]:
        scope, params = scope_sql(tenant_id, fleet_id)
        params["stale_days"] = stale_days
        params["max_weight"] = max_weight
        result = await db.execute(
            text(f"""
            SELECT m.id
            FROM memories m
            WHERE {scope}
              AND m.created_at < NOW() - INTERVAL '1 day' * :stale_days
              AND m.recall_count = 0
              AND m.weight < :max_weight
              AND m.deleted_at IS NULL
            LIMIT 100
        """),
            params,
        )
        return result.all()

    async def find_short_content(
        self,
        db: AsyncSession,
        tenant_id: str,
        fleet_id: str | None,
        min_chars: int,
    ) -> list[tuple]:
        scope, params = scope_sql(tenant_id, fleet_id)
        params["min_chars"] = min_chars
        result = await db.execute(
            text(f"""
            SELECT m.id
            FROM memories m
            WHERE {scope}
              AND LENGTH(m.content) < :min_chars
              AND m.deleted_at IS NULL
            LIMIT 100
        """),
            params,
        )
        return result.all()

    async def compute_health_stats(
        self,
        db: AsyncSession,
        tenant_id: str,
        fleet_id: str | None,
    ) -> dict:
        scope, params = scope_sql(tenant_id, fleet_id)

        r = await db.execute(
            text(f"""
            SELECT COUNT(*) FROM memories m WHERE {scope} AND m.deleted_at IS NULL
        """),
            params,
        )
        total = r.scalar() or 0

        r = await db.execute(
            text(f"""
            SELECT COUNT(*) FROM memories m
            WHERE {scope} AND m.deleted_at IS NULL AND m.embedding IS NOT NULL
        """),
            params,
        )
        with_embedding = r.scalar() or 0
        embedding_pct = round(with_embedding / total * 100, 1) if total > 0 else 0.0

        r = await db.execute(
            text(f"""
            SELECT m.memory_type, COUNT(*) AS cnt
            FROM memories m
            WHERE {scope} AND m.deleted_at IS NULL
            GROUP BY m.memory_type
            ORDER BY cnt DESC
        """),
            params,
        )
        type_dist = {row[0]: row[1] for row in r.all()}

        r = await db.execute(
            text(f"""
            SELECT m.status, COUNT(*) AS cnt
            FROM memories m
            WHERE {scope} AND m.deleted_at IS NULL
            GROUP BY m.status
            ORDER BY cnt DESC
        """),
            params,
        )
        status_dist = {row[0]: row[1] for row in r.all()}

        r = await db.execute(
            text(f"""
            SELECT AVG(m.weight),
                   PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY m.weight),
                   PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY m.weight)
            FROM memories m
            WHERE {scope} AND m.deleted_at IS NULL
        """),
            params,
        )
        wrow = r.one()
        weight_stats = {
            "avg": round(float(wrow[0]), 3) if wrow[0] is not None else None,
            "p50": round(float(wrow[1]), 3) if wrow[1] is not None else None,
            "p90": round(float(wrow[2]), 3) if wrow[2] is not None else None,
        }

        r = await db.execute(
            text(f"""
            SELECT COUNT(*) FROM memories m
            WHERE {scope} AND m.deleted_at IS NULL AND m.status IN ('outdated', 'conflicted')
        """),
            params,
        )
        contradiction_count = r.scalar() or 0

        r = await db.execute(
            text(f"""
            SELECT COUNT(*) FROM memories m
            WHERE {scope} AND m.deleted_at IS NULL AND m.metadata->>'contains_pii' = 'true'
        """),
            params,
        )
        pii_count = r.scalar() or 0

        r = await db.execute(
            text(f"""
            SELECT AVG(m.recall_count) FROM memories m
            WHERE {scope} AND m.deleted_at IS NULL
        """),
            params,
        )
        avg_recall = r.scalar()
        avg_recall = round(float(avg_recall), 2) if avg_recall is not None else 0.0

        return {
            "total_memories": total,
            "embedding_coverage_pct": embedding_pct,
            "type_distribution": type_dist,
            "status_distribution": status_dist,
            "weight_stats": weight_stats,
            "contradiction_count": contradiction_count,
            "pii_count": pii_count,
            "avg_recall_count": avg_recall,
        }

    async def compute_usage_stats(
        self,
        db: AsyncSession,
        tenant_id: str,
        fleet_id: str | None,
    ) -> dict:
        scope, params = scope_sql(tenant_id, fleet_id)

        r = await db.execute(
            text(f"""
            SELECT m.id, m.title, m.recall_count
            FROM memories m
            WHERE {scope} AND m.deleted_at IS NULL
            ORDER BY m.recall_count DESC
            LIMIT 10
        """),
            params,
        )
        most_recalled = [{"id": str(row[0]), "title": row[1], "recall_count": row[2]} for row in r.all()]

        r = await db.execute(
            text(f"""
            SELECT m.id, m.title, m.recall_count
            FROM memories m
            WHERE {scope} AND m.deleted_at IS NULL AND m.status = 'active'
            ORDER BY m.recall_count ASC
            LIMIT 10
        """),
            params,
        )
        least_recalled = [{"id": str(row[0]), "title": row[1], "recall_count": row[2]} for row in r.all()]

        r = await db.execute(
            text(f"""
            SELECT m.fleet_id, COUNT(*) AS cnt
            FROM memories m
            WHERE {scope} AND m.deleted_at IS NULL
            GROUP BY m.fleet_id
            ORDER BY cnt DESC
        """),
            params,
        )
        fleet_activity = [{"fleet_id": row[0], "memory_count": row[1]} for row in r.all()]

        return {
            "most_recalled": most_recalled,
            "least_recalled": least_recalled,
            "fleet_activity": fleet_activity,
        }

    # ------------------------------------------------------------------
    # G) Recall tracking
    # ------------------------------------------------------------------

    async def increment_recall(self, db: AsyncSession, memory_ids: list[UUID]) -> None:
        if not memory_ids:
            return
        await db.execute(
            sql_update(Memory)
            .where(Memory.id.in_(memory_ids))
            .values(
                recall_count=Memory.recall_count + 1,
                last_recalled_at=func.now(),
            )
        )

    # ------------------------------------------------------------------
    # H) Entity links (memory side)
    # ------------------------------------------------------------------

    async def add_entity_link(
        self,
        db: AsyncSession,
        memory_id: UUID,
        entity_id: UUID,
        role: str,
    ) -> None:
        db.add(MemoryEntityLink(memory_id=memory_id, entity_id=entity_id, role=role))

    async def get_entity_links_for_memories(
        self,
        db: AsyncSession,
        memory_ids: list[UUID],
    ) -> dict[UUID, list]:
        if not memory_ids:
            return {}
        result = await db.execute(select(MemoryEntityLink).where(MemoryEntityLink.memory_id.in_(memory_ids)))
        links_by_memory: dict[UUID, list] = {}
        for link in result.scalars().all():
            links_by_memory.setdefault(link.memory_id, []).append(
                EntityLinkOut(entity_id=link.entity_id, role=link.role)
            )
        return links_by_memory

    async def get_memories_by_ids(
        self,
        db: AsyncSession,
        memory_ids: list[UUID],
    ) -> dict[UUID, Memory]:
        """Fetch multiple memories by ID, returned as {id: Memory}."""
        if not memory_ids:
            return {}
        stmt = select(Memory).where(
            Memory.id.in_(memory_ids),
            Memory.deleted_at.is_(None),
        )
        result = await db.execute(stmt)
        return {m.id: m for m in result.scalars().all()}
