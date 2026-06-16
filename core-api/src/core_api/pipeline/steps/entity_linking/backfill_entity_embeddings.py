"""BackfillEntityEmbeddings — generate name_embedding for entities that lack one."""

from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy import text, update

from common.embedding import get_embedding
from common.models.entity import Entity
from core_api.constants import ENTITY_EMBEDDING_BACKFILL_BATCH_SIZE
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.step import StepOutcome, StepResult

logger = logging.getLogger(__name__)


class BackfillEntityEmbeddings:
    @property
    def name(self) -> str:
        return "backfill_entity_embeddings"

    async def execute(self, ctx: PipelineContext) -> StepResult | None:
        """Embed entities whose name_embedding is NULL."""
        tenant_id: str = ctx.data["tenant_id"]
        fleet_id: str | None = ctx.data.get("fleet_id")
        batch_size: int = ctx.data.get(
            "entity_embedding_backfill_batch_size",
            ENTITY_EMBEDDING_BACKFILL_BATCH_SIZE,
        )

        fleet_clause = "AND fleet_id = :fleet_id" if fleet_id else ""
        rows = (
            await ctx.require_db.execute(
                text(f"""
                    SELECT id, canonical_name
                    FROM entities
                    WHERE tenant_id = :tenant_id
                      AND name_embedding IS NULL
                      {fleet_clause}
                    LIMIT :batch_size
                """),
                {
                    "tenant_id": tenant_id,
                    **({"fleet_id": fleet_id} if fleet_id else {}),
                    "batch_size": batch_size,
                },
            )
        ).all()

        if not rows:
            return StepResult(outcome=StepOutcome.SKIPPED)

        updates: list[dict] = []
        for eid, canonical_name in rows:
            try:
                embedding = await get_embedding(canonical_name, ctx.tenant_config)
            except Exception:
                logger.warning("Failed to embed entity %s (%s)", eid, canonical_name, exc_info=True)
                continue

            if embedding is not None:
                updates.append({"eid": eid, "emb": embedding})

        backfill_count = 0
        if updates:
            await ctx.require_db.execute(
                # Target the Core ``entities`` table, NOT the ORM-mapped ``Entity``.
                # ``session.execute(update(Entity), <list of param dicts>)`` routes to
                # SQLAlchemy's "ORM Bulk UPDATE by Primary Key", which requires every
                # dict to carry the PK column ``id`` — but our dicts key the PK off a
                # custom ``eid`` bindparam in the WHERE clause, so that path raised
                # ``InvalidRequestError: No primary key value supplied for column(s)
                # entities.id`` (prod 2026-06-16). The earlier ``synchronize_session=
                # False`` (prod 2026-06-13) only silenced a *different* error on that
                # same ORM path. ``update(Entity.__table__)`` is a plain Core executemany
                # UPDATE that honours the custom bindparams and has no ORM bulk-by-PK or
                # session-synchronisation behaviour at all.
                update(Entity.__table__)
                .where(
                    Entity.__table__.c.id == sa.bindparam("eid"),
                    Entity.__table__.c.tenant_id == tenant_id,
                )
                .values(name_embedding=sa.bindparam("emb")),
                updates,
            )
            backfill_count = len(updates)

        await ctx.require_db.flush()
        ctx.data["backfill_count"] = backfill_count

        logger.info(
            "Backfilled %d/%d entity embeddings for tenant %s",
            backfill_count,
            len(rows),
            tenant_id,
        )
        return StepResult(
            outcome=StepOutcome.SUCCESS,
            detail={"backfill_count": backfill_count},
        )
