"""Mark text-mined cross-links as extraction's (M-65, O13, after migration 048).

Migration 048 gave ``memory_entity_links`` a ``source`` so that a content edit
clears the links extraction mined from the old text and keeps the ones a caller
added. ``entity_discover_cross_links`` wrote no ``source`` until caura PR #1776,
so every cross-link it mined from backend-v3.16.0 to v3.21.0 reads ``caller``
and outlives every edit. This marks them ``extraction``, so the next content
edit clears them with the rest of extraction's links (owner decision
2026-10-13).

    python -m core_storage_api.scripts.backfill_048_cross_link_source
    python -m core_storage_api.scripts.backfill_048_cross_link_source --tenant-id <tenant>
    python -m core_storage_api.scripts.backfill_048_cross_link_source --apply

A dry run is the default: it counts and changes nothing. ``--apply`` writes.

A link carries no date and no writer, so the test is discovery's own: a
``caller`` link with role ``mentioned`` whose entity's name or an alias appears,
ignoring case, in its memory's current text. No rule can tell such a link from
one a caller added with role ``mentioned`` for an entity the text names; that
one is marked too, and the memory's next content edit clears it. A link whose
entity the text does not name stays ``caller``: that is a caller's tag for an
entity its text never names, which 048 protects, and a mined link an edit
already left stale looks the same, so it stays too. The test reaches links from
before 048 as well, which read ``caller`` by its default.

Only a live memory's links, and only to an entity of the memory's tenant. An
empty name or alias matches nothing here, though discovery's substring test
matched every text with one.

Safe to re-run, to interrupt and to run while serving. It walks ``memories`` by
id, one batch per transaction, and each batch logs the last id it covered;
``--after-id`` resumes from there.
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

from common.models import Entity, Memory, MemoryEntityLink
from common.models.entity import LINK_SOURCE_CALLER, LINK_SOURCE_EXTRACTION
from core_storage_api.database.init import get_engine

logger = logging.getLogger("backfill_048")

# The role ``entity_discover_cross_links`` writes on every link.
MINED_ROLE = "mentioned"


@dataclass
class Run:
    """What one walk over ``memories`` found."""

    memories: int = 0
    candidates: int = 0  # caller links with the mined role
    marked: int = 0  # of those, marked extraction, or on a dry run, that would be
    last_id: uuid.UUID | None = None


def _walk(*, tenant_id: str | None, after: uuid.UUID | None, batch: int) -> sa.Select:
    """The next ``batch`` live memory ids after ``after``."""
    stmt = sa.select(Memory.id).where(Memory.deleted_at.is_(None)).order_by(Memory.id).limit(batch)
    if tenant_id is not None:
        stmt = stmt.where(Memory.tenant_id == tenant_id)
    if after is not None:
        stmt = stmt.where(Memory.id > after)
    return stmt


def _candidates(ids: list[uuid.UUID]) -> sa.Select:
    """The ``caller`` links with the mined role on the memories in ``ids``, each
    with its memory's text and its entity's names."""
    return (
        sa.select(
            MemoryEntityLink.memory_id,
            MemoryEntityLink.entity_id,
            Memory.content,
            Entity.canonical_name,
            Entity.attributes,
        )
        .join(Memory, Memory.id == MemoryEntityLink.memory_id)
        .join(Entity, Entity.id == MemoryEntityLink.entity_id)
        .where(
            MemoryEntityLink.memory_id.in_(ids),
            MemoryEntityLink.source == LINK_SOURCE_CALLER,
            MemoryEntityLink.role == MINED_ROLE,
            Entity.tenant_id == Memory.tenant_id,
        )
    )


def _names_its_entity(content: str | None, canonical_name: str | None, attributes: object) -> bool:
    """Discovery's text test: the entity's name or an alias is in ``content``,
    ignoring case. Empty and non-string names match nothing."""
    names = [canonical_name]
    if isinstance(attributes, dict) and isinstance(attributes.get("_aliases"), list):
        names.extend(attributes["_aliases"])
    text = (content or "").lower()
    return any(isinstance(name, str) and name.strip() and name.lower() in text for name in names)


async def _mark(conn: AsyncConnection, ids: list[uuid.UUID], *, apply: bool) -> tuple[int, int]:
    """Mark the mined links on the memories in ``ids``. Returns how many caller
    links had the mined role, and how many of those it marked, or on a dry run,
    would mark."""
    rows = (await conn.execute(_candidates(ids))).all()
    mined = [
        (row.memory_id, row.entity_id)
        for row in rows
        if _names_its_entity(row.content, row.canonical_name, row.attributes)
    ]
    if apply and mined:
        await conn.execute(
            sa.update(MemoryEntityLink)
            .where(
                sa.tuple_(MemoryEntityLink.memory_id, MemoryEntityLink.entity_id).in_(mined),
                MemoryEntityLink.source == LINK_SOURCE_CALLER,
            )
            .values(source=LINK_SOURCE_EXTRACTION)
        )
    return len(rows), len(mined)


async def _run(*, tenant_id: str | None, batch: int, apply: bool, after_id: uuid.UUID | None = None) -> Run:
    logger.info(
        "%s; tenant=%s; after=%s",
        "marking" if apply else "dry run (pass --apply to write)",
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
            candidates, marked = await _mark(conn, ids, apply=apply)
        found.memories += len(ids)
        found.candidates += candidates
        found.marked += marked
        found.last_id = ids[-1]
        logger.info(
            "... %s memories, %s caller '%s' links, %s %s; resume with --after-id %s",
            found.memories,
            found.candidates,
            MINED_ROLE,
            found.marked,
            "marked" if apply else "to mark",
            found.last_id,
        )
    logger.info(
        "done: %s memories, %s caller '%s' links, %s %s",
        found.memories,
        found.candidates,
        MINED_ROLE,
        found.marked,
        "marked" if apply else "to mark",
    )
    return found


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="core_storage_api.scripts.backfill_048_cross_link_source")
    p.add_argument("--tenant-id", default=None, help="only this tenant's memories")
    p.add_argument("--batch", type=int, default=1000, help="memories per transaction")
    p.add_argument("--apply", action="store_true", help="write; without it, count and change nothing")
    p.add_argument("--after-id", type=uuid.UUID, default=None, help="resume after this memory id")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main() -> None:
    args = _args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    try:
        asyncio.run(
            _run(tenant_id=args.tenant_id, batch=args.batch, apply=args.apply, after_id=args.after_id)
        )
    except KeyboardInterrupt:
        logger.error("interrupted; run it again with the last --after-id logged")
        sys.exit(130)


if __name__ == "__main__":
    main()
