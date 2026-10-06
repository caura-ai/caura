"""Date entities from their earliest linked memory (H-05, O17, after migration 060).

The nightly duplicate merge keeps the first seen of two compatible unqualified
names, by ``entities.created_at`` (caura PR #1876). Migration 060 added that
column with ``DEFAULT now()``, so every entity written before it reads the
migration's time, and between those the merge falls back to the longer name.
This dates each entity from the earliest memory linked to it, the closest record
there is of when it was first seen (owner decision 2026-10-06).

    python -m core_storage_api.scripts.backfill_060_entity_created_at --dry-run
    python -m core_storage_api.scripts.backfill_060_entity_created_at --tenant-id <tenant>
    python -m core_storage_api.scripts.backfill_060_entity_created_at

Run it out of band, after the storage deploy that runs migration 060 is healthy,
the way ``backfill_058_orphaned_derived_rows`` runs after 058. It walks
``entities`` by id, one batch per transaction, and reads each batch's links
through ``ix_memory_entity_links_entity_id``.

It only ever moves a time earlier. An entity written after 060 is dated at its
insert, moments after the memory that named it, so it moves by moments if at
all; an entity no memory links keeps the time it has. A memory counts only in
the entity's own tenant, and whatever its status: one deleted since still shows
when the entity was first seen.

Safe to re-run, to interrupt and to run while serving. Each batch is its own
transaction, and a re-run finds nothing earlier to set on a row already dated.
Each batch logs the last id it covered; ``--after-id`` resumes from there.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.orm import aliased

from common.models import Entity, Memory, MemoryEntityLink
from core_storage_api.database.init import get_engine

logger = logging.getLogger("backfill_060")


@dataclass
class Run:
    """What one walk over ``entities`` found."""

    entities: int = 0
    dated: int = 0  # entities given an earlier created_at, or on a dry run, that would be
    last_id: uuid.UUID | None = None


def _walk(*, tenant_id: str | None, after: uuid.UUID | None, batch: int) -> sa.Select:
    """The next ``batch`` entity ids after ``after``."""
    stmt = sa.select(Entity.id).order_by(Entity.id).limit(batch)
    if tenant_id is not None:
        stmt = stmt.where(Entity.tenant_id == tenant_id)
    if after is not None:
        stmt = stmt.where(Entity.id > after)
    return stmt


def _first_seen(ids: list[uuid.UUID]) -> sa.Subquery:
    """Each entity in ``ids`` a memory of its own tenant links, with the earliest
    such memory's ``created_at``."""
    owner = aliased(Entity)
    return (
        sa.select(MemoryEntityLink.entity_id, sa.func.min(Memory.created_at).label("first_seen"))
        .join(Memory, Memory.id == MemoryEntityLink.memory_id)
        .join(owner, owner.id == MemoryEntityLink.entity_id)
        .where(MemoryEntityLink.entity_id.in_(ids), Memory.tenant_id == owner.tenant_id)
        .group_by(MemoryEntityLink.entity_id)
        .subquery()
    )


async def _date(conn: AsyncConnection, ids: list[uuid.UUID], *, dry_run: bool) -> int:
    """Move each entity in ``ids`` back to its first-seen time, where that is
    earlier. Returns how many it moved, or on a dry run, would move."""
    seen = _first_seen(ids)
    earlier = seen.c.first_seen < Entity.created_at
    if dry_run:
        count = (
            sa.select(sa.func.count())
            .select_from(seen)
            .join(Entity, Entity.id == seen.c.entity_id)
            .where(earlier)
        )
        return (await conn.execute(count)).scalar_one()
    result = await conn.execute(
        sa.update(Entity).where(Entity.id == seen.c.entity_id, earlier).values(created_at=seen.c.first_seen)
    )
    return result.rowcount or 0


async def _run(*, tenant_id: str | None, batch: int, dry_run: bool, after_id: uuid.UUID | None = None) -> Run:
    logger.info(
        "%s; tenant=%s; after=%s",
        "dry run" if dry_run else "dating",
        tenant_id or "all",
        after_id or "the first id",
    )
    found = Run(last_id=after_id)
    while True:
        async with get_engine().begin() as conn:
            rows = await conn.execute(_walk(tenant_id=tenant_id, after=found.last_id, batch=batch))
            ids = list(rows.scalars())
            if not ids:
                break
            found.dated += await _date(conn, ids, dry_run=dry_run)
        found.entities += len(ids)
        found.last_id = ids[-1]
        logger.info(
            "... %s entities, %s %s; resume with --after-id %s",
            found.entities,
            found.dated,
            "to date" if dry_run else "dated",
            found.last_id,
        )
    logger.info("done: %s entities, %s %s", found.entities, found.dated, "to date" if dry_run else "dated")
    return found


def main() -> None:
    p = argparse.ArgumentParser(prog="core_storage_api.scripts.backfill_060_entity_created_at")
    p.add_argument("--tenant-id", default=None, help="only this tenant's entities")
    p.add_argument("--batch", type=int, default=1000, help="entities per transaction")
    p.add_argument("--dry-run", action="store_true", help="count, change nothing")
    p.add_argument("--after-id", type=uuid.UUID, default=None, help="resume after this entity id")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    try:
        asyncio.run(
            _run(tenant_id=args.tenant_id, batch=args.batch, dry_run=args.dry_run, after_id=args.after_id)
        )
    except KeyboardInterrupt:
        logger.error("interrupted; run it again with the last --after-id logged")
        sys.exit(130)


if __name__ == "__main__":
    main()
