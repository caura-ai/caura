"""Soft-delete the rows derived from a parent that is gone (O12, after migration 058).

Auto-chunk and atomic-fact children link to their parent only through
``metadata.parent_memory_id``, and carry its text. Since caura PR #1843 every
delete takes the live rows derived from what it deletes (B25, M-52 and M-53).
Rows whose parent was deleted before that, or never existed, are still live: the
deleted text is still recalled through them, and no later delete reaches them
because their parent is already gone. This finds them and deletes them the way
the cascade would have (owner decision 2026-10-05).

    python -m core_storage_api.scripts.backfill_058_orphaned_derived_rows --dry-run
    python -m core_storage_api.scripts.backfill_058_orphaned_derived_rows --tenant-id <tenant>
    python -m core_storage_api.scripts.backfill_058_orphaned_derived_rows

Run it out of band, after the storage deploy that runs migration 058 is healthy,
the way ``backfill_057_entity_search_vector`` runs after 057. It walks 058's
partial index, which holds only live derived rows, one batch of distinct
``(tenant_id, parent)`` keys at a time.

Gone means the parent id names no live row in the child's own tenant: a
soft-deleted row, one never written, or a value that is not a memory id at all.
Tenant-keyed, like ``derived_rows_where``: the cascade never crosses tenants.

What happens to a row is exactly the cascade's soft delete, through the
cascade's own predicate (``derived_rows_where``): ``deleted_at`` is set and
``status`` becomes ``'deleted'``. The previous status is not kept, as no delete
keeps it. Every row of one run gets the same ``deleted_at``, logged at the
start, so the rows a run took can be found afterwards.

Rows derived from a row this deletes (an atomic fact of an auto-chunk child) are
orphans in turn, so the walk repeats until a pass finds nothing. ``--dry-run``
makes one pass and changes nothing, so it counts the first level only.

Safe to re-run, to interrupt and to run while serving. Each batch is its own
transaction, and a row the service deletes meanwhile simply stops matching. An
interrupted run is resumed by running it again: what it already deleted has left
the index.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from common.models import Memory
from core_storage_api.database.init import get_engine
from core_storage_api.services.postgres_service import derived_rows_where

logger = logging.getLogger("backfill_058")

# Migration 058's index expression, spelled as the index spells it, so the walk
# below reads that index.
_PARENT = sa.literal_column("(metadata ->> 'parent_memory_id')", sa.Text())


@dataclass
class Pass:
    """What one walk over the index found."""

    keys: int = 0
    deleted_parents: int = 0
    missing_parents: int = 0
    rows: int = 0  # rows deleted, or on a dry run, rows that would be


def _walk(*, tenant_id: str | None, after: tuple[str, str] | None, batch: int) -> sa.Select:
    """The next ``batch`` distinct ``(tenant_id, parent)`` keys after ``after``."""
    stmt = (
        sa.select(Memory.tenant_id, _PARENT)
        .where(Memory.deleted_at.is_(None), _PARENT.is_not(None))
        .distinct()
        .order_by(Memory.tenant_id, _PARENT)
        .limit(batch)
    )
    if tenant_id is not None:
        stmt = stmt.where(Memory.tenant_id == tenant_id)
    if after is not None:
        cursor = sa.tuple_(sa.literal(after[0]), sa.literal(after[1]))
        stmt = stmt.where(sa.tuple_(Memory.tenant_id, _PARENT) > cursor)
    return stmt


def _as_uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def _gone(conn: AsyncConnection, keys: list[tuple[str, str]], found: Pass) -> dict[str, list[str]]:
    """The keys whose parent is gone, as ``{tenant_id: [parent, ...]}``, counting
    deleted and missing parents into ``found``. A parent is kept as the child
    stores it, so the delete matches the stored text exactly."""
    ids = list({parsed for _, parent in keys if (parsed := _as_uuid(parent)) is not None})
    live: dict[tuple[str, str], bool] = {}
    if ids:
        rows = await conn.execute(
            sa.select(Memory.tenant_id, Memory.id, Memory.deleted_at.is_(None)).where(Memory.id.in_(ids))
        )
        live = {(tenant, str(memory_id)): is_live for tenant, memory_id, is_live in rows}
    gone: dict[str, list[str]] = {}
    for tenant, parent in keys:
        parsed = _as_uuid(parent)
        state = live.get((tenant, str(parsed))) if parsed is not None else None
        if state is True:
            continue
        if state is False:
            found.deleted_parents += 1
        else:
            found.missing_parents += 1
        gone.setdefault(tenant, []).append(parent)
    return gone


async def _pass(*, tenant_id: str | None, batch: int, dry_run: bool, run_at: datetime) -> Pass:
    found = Pass()
    after: tuple[str, str] | None = None
    while True:
        async with get_engine().begin() as conn:
            rows = await conn.execute(_walk(tenant_id=tenant_id, after=after, batch=batch))
            keys = [(tenant, parent) for tenant, parent in rows]
            if not keys:
                return found
            after = keys[-1]
            found.keys += len(keys)
            for tenant, parents in (await _gone(conn, keys, found)).items():
                where = derived_rows_where(tenant, parents)
                if dry_run:
                    count = sa.select(sa.func.count()).select_from(Memory).where(*where)
                    found.rows += (await conn.execute(count)).scalar_one()
                else:
                    result = await conn.execute(
                        sa.update(Memory).where(*where).values(deleted_at=run_at, status="deleted")
                    )
                    found.rows += result.rowcount or 0
        logger.debug("... %s keys, %s rows, cursor %s", found.keys, found.rows, after)


async def _run(*, tenant_id: str | None, batch: int, dry_run: bool, max_passes: int) -> list[Pass]:
    run_at = datetime.now(UTC)
    logger.info(
        "%s; tenant=%s; deleted_at for this run: %s",
        "dry run" if dry_run else "deleting",
        tenant_id or "all",
        run_at.isoformat(),
    )
    passes: list[Pass] = []
    for number in range(1, max_passes + 1):
        found = await _pass(tenant_id=tenant_id, batch=batch, dry_run=dry_run, run_at=run_at)
        passes.append(found)
        logger.info(
            "pass %s: %s parent keys, %s deleted and %s missing parents, %s rows %s",
            number,
            found.keys,
            found.deleted_parents,
            found.missing_parents,
            found.rows,
            "to delete" if dry_run else "deleted",
        )
        if dry_run or found.rows == 0:
            break
    else:
        logger.warning(
            "stopped after %s passes while rows were still being deleted; run it again", max_passes
        )
    return passes


def main() -> None:
    p = argparse.ArgumentParser(prog="core_storage_api.scripts.backfill_058_orphaned_derived_rows")
    p.add_argument("--tenant-id", default=None, help="only this tenant's rows")
    p.add_argument("--batch", type=int, default=1000, help="parent keys per transaction")
    p.add_argument("--dry-run", action="store_true", help="count the first level, change nothing")
    p.add_argument("--max-passes", type=int, default=10)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    try:
        asyncio.run(
            _run(tenant_id=args.tenant_id, batch=args.batch, dry_run=args.dry_run, max_passes=args.max_passes)
        )
    except KeyboardInterrupt:
        logger.error("interrupted; run it again to finish")
        sys.exit(130)


if __name__ == "__main__":
    main()
