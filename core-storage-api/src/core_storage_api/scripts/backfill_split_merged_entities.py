"""Split the entities the old nightly merge collapsed (H-05, O2).

Before caura PR #1876 the nightly duplicate merge (``entity_resolve_duplicates``)
joined same-type entities by name-embedding similarity alone, with no identifier
or qualifier guard: 'comet #0002' and 'comet #0012', or 'acme (delaware)' and
'acme (ohio)', became one entity. The duplicate's links moved to the survivor,
its row was deleted, and its name survives only among the survivor's
``attributes._aliases``. The write path took on the same guard in stages, so its
earlier merges look the same.

Such an entity holds names ``same_identifier_signature`` keeps apart. This groups
its names by their identifier tokens and bracketed qualifiers and, for each group
but the canonical name's, moves every live link whose memory names that group and
no other to an entity of its own (owner decision 2026-10-13). That entity is the
tenant's existing one of a group name, type and fleet, or a new one named by the
group's first name and dated from its earliest moved memory. A moved memory whose
subject was the old entity takes the new one. The group's names join the new
entity's aliases, and leave the old entity's once no link left there names them.
A memory names a group when its text holds one of the group's names whole,
ignoring case: not inside a longer word, nor followed by more of an identifier
('comet #1' is not in 'comet #12' or 'comet #1.5').

A name with neither identifier nor qualifier ('comet') is a part of the others
and decides nothing. What stays: a link whose memory names several groups, or
none, or only the canonical one; a group no link moves to; relations, which no
memory tells apart. A new entity has no name embedding until the nightly
``BackfillEntityEmbeddings`` step gives it one. A split by qualifier can be
undone in part: the nightly merge may still join a plain 'acme' to one
'acme (ohio)', as the write path may.

    python -m core_storage_api.scripts.backfill_split_merged_entities --dry-run
    python -m core_storage_api.scripts.backfill_split_merged_entities --tenant-id <tenant>
    python -m core_storage_api.scripts.backfill_split_merged_entities

Safe to re-run, to interrupt and to run while serving. It walks ``entities`` by
id; each entity it splits is one transaction holding that entity's row lock. A
second run moves nothing: what a memory names does not change, and a group's
names leave the old entity only once no link there names them. Each batch logs
the last id it covered; ``--after-id`` resumes from there.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from itertools import batched
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.orm import aliased

from common.entity_naming import identifier_tokens, qualifier_signature
from common.models import Entity, Memory, MemoryEntityLink
from core_storage_api.database.init import get_engine
from core_storage_api.services.postgres_service import _merge_entity_attributes

logger = logging.getLogger("backfill_split")

# An entity with aliases, read as the search-vector trigger reads them (migration
# 057): ``attributes`` is ``json``, and a value that is not an array is none.
_HAS_ALIASES = sa.text(
    "json_typeof(entities.attributes->'_aliases') = 'array' "
    "AND json_array_length(entities.attributes->'_aliases') > 0"
)
_ROWS_PER_STATEMENT = 1000

# What a split reads of the entity it splits.
_OLD = (Entity.tenant_id, Entity.fleet_id, Entity.entity_type, Entity.canonical_name, Entity.attributes)

# What ``same_identifier_signature`` compares: identifier tokens, then
# bracketed qualifiers.
Key = tuple[frozenset[str], frozenset[str]]
_PLAIN: Key = (frozenset(), frozenset())


@dataclass
class Run:
    """What one walk over ``entities`` found."""

    entities: int = 0  # entities with aliases
    conflicting: int = 0  # of those, holding names the guard keeps apart
    split: int = 0  # entities a link moved off, or on a dry run, would
    moved: int = 0  # links moved, or on a dry run, that would be
    new_entities: int = 0  # entities made for a group, or that would be
    existing_entities: int = 0  # the tenant's own entities a group's links moved to
    last_id: uuid.UUID | None = None


def _key(name: str) -> Key:
    return identifier_tokens(name), qualifier_signature(name)


def _names(canonical_name: str, attributes: Any) -> list[str]:
    """The canonical name, then each alias, once."""
    aliases = attributes.get("_aliases") if isinstance(attributes, dict) else None
    names = [canonical_name]
    for alias in aliases if isinstance(aliases, list) else []:
        if isinstance(alias, str) and alias not in names:
            names.append(alias)
    return names


def _conflicting(names: list[str]) -> bool:
    """Whether two of ``names`` fail ``same_identifier_signature``: their
    identifier tokens differ, or both carry qualifiers and those differ."""
    keys = [_key(name) for name in names]
    return len({tokens for tokens, _ in keys}) > 1 or len({quals for _, quals in keys if quals}) > 1


def _groups(names: list[str]) -> dict[Key, list[str]]:
    """``names`` by key, but for a name with neither identifier nor qualifier,
    which is a part of the others ('comet' of 'comet #0012')."""
    groups: dict[Key, list[str]] = {}
    for name in names:
        if (key := _key(name)) != _PLAIN:
            groups.setdefault(key, []).append(name)
    return groups


def _pattern(names: list[str]) -> re.Pattern[str]:
    """Any of ``names`` whole in lowered text. After it, neither a word character
    nor a '.' or '-' that goes on to one, which ``identifier_tokens`` would read
    as the same token."""
    alternatives = "|".join(re.escape(name.lower()) for name in names)
    return re.compile(rf"(?<!\w)(?:{alternatives})(?!\w|[.\-]\w)")


def _walk(*, tenant_id: str | None, after: uuid.UUID | None, batch: int) -> sa.Select:
    """The next ``batch`` entities with aliases after ``after``."""
    stmt = (
        sa.select(Entity.id, Entity.canonical_name, Entity.attributes)
        .where(_HAS_ALIASES)
        .order_by(Entity.id)
        .limit(batch)
    )
    if tenant_id is not None:
        stmt = stmt.where(Entity.tenant_id == tenant_id)
    if after is not None:
        stmt = stmt.where(Entity.id > after)
    return stmt


async def _live_links(conn: AsyncConnection, entity_id: uuid.UUID, tenant_id: str) -> AsyncIterator[Any]:
    """Each live memory of the entity's own tenant that links it."""
    after: uuid.UUID | None = None
    while True:
        stmt = (
            sa.select(MemoryEntityLink.memory_id, Memory.content, Memory.created_at)
            .join(Memory, Memory.id == MemoryEntityLink.memory_id)
            .where(
                MemoryEntityLink.entity_id == entity_id,
                Memory.tenant_id == tenant_id,
                Memory.deleted_at.is_(None),
            )
            .order_by(MemoryEntityLink.memory_id)
            .limit(_ROWS_PER_STATEMENT)
        )
        if after is not None:
            stmt = stmt.where(MemoryEntityLink.memory_id > after)
        rows = (await conn.execute(stmt)).all()
        for row in rows:
            yield row
        if len(rows) < _ROWS_PER_STATEMENT:
            return
        after = rows[-1].memory_id


async def _existing(conn: AsyncConnection, old: Any, names: list[str], *, lock: bool) -> Any:
    """The tenant's entity of the old one's type and fleet called one of ``names``."""
    stmt = (
        sa.select(Entity.id, Entity.attributes)
        .where(
            Entity.tenant_id == old.tenant_id,
            Entity.entity_type == old.entity_type,
            sa.func.coalesce(Entity.fleet_id, "") == (old.fleet_id or ""),
            sa.func.lower(Entity.canonical_name).in_([name.lower() for name in names]),
        )
        .order_by(Entity.created_at, Entity.id)
        .limit(1)
    )
    if lock:
        stmt = stmt.with_for_update(key_share=True)
    return (await conn.execute(stmt)).one_or_none()


async def _target(
    conn: AsyncConnection, old: Any, names: list[str], first_seen: datetime, *, dry_run: bool, found: Run
) -> uuid.UUID | None:
    """The entity a group's links move to; ``None`` on a dry run that would make it."""
    target = await _existing(conn, old, names, lock=not dry_run)
    if target is None:
        if dry_run:
            found.new_entities += 1
            return None
        # ``uq_entities_tenant_type_name_fleet``, inferred as ``entity_add`` infers it.
        made = await conn.execute(
            pg_insert(Entity)
            .values(
                tenant_id=old.tenant_id,
                fleet_id=old.fleet_id,
                entity_type=old.entity_type,
                canonical_name=names[0],
                attributes={"_aliases": names},
                created_at=first_seen,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    Entity.tenant_id,
                    Entity.entity_type,
                    sa.text("lower(canonical_name)"),
                    sa.text("COALESCE(fleet_id, '')"),
                ]
            )
            .returning(Entity.id)
        )
        if (made_id := made.scalar_one_or_none()) is not None:
            found.new_entities += 1
            return made_id
        # A writer made it meanwhile.
        target = await _existing(conn, old, names, lock=True)
        if target is None:
            raise RuntimeError(f"entity {names[0]!r} conflicted on insert and then could not be read")
    found.existing_entities += 1
    if not dry_run:
        await conn.execute(
            sa.update(Entity)
            .where(Entity.id == target.id)
            .values(
                attributes=_merge_entity_attributes(target.attributes, {"_aliases": names}),
                created_at=sa.func.least(Entity.created_at, first_seen),
            )
        )
    return target.id


async def _move(
    conn: AsyncConnection,
    old_id: uuid.UUID,
    target_id: uuid.UUID,
    tenant_id: str,
    memory_ids: list[uuid.UUID],
) -> None:
    """Move these memories' links, and their subject, from the old entity to the target."""
    theirs = aliased(MemoryEntityLink)
    already_linked = sa.select(theirs.memory_id).where(theirs.entity_id == target_id)
    for chunk in batched(memory_ids, _ROWS_PER_STATEMENT):
        # The key is (memory_id, entity_id): a memory the target already links keeps that link.
        await conn.execute(
            sa.delete(MemoryEntityLink).where(
                MemoryEntityLink.entity_id == old_id,
                MemoryEntityLink.memory_id.in_(chunk),
                MemoryEntityLink.memory_id.in_(already_linked),
            )
        )
        await conn.execute(
            sa.update(MemoryEntityLink)
            .where(MemoryEntityLink.entity_id == old_id, MemoryEntityLink.memory_id.in_(chunk))
            .values(entity_id=target_id)
        )
        await conn.execute(
            sa.update(Memory)
            .where(Memory.id.in_(chunk), Memory.tenant_id == tenant_id, Memory.subject_entity_id == old_id)
            .values(subject_entity_id=target_id)
        )


async def _split(conn: AsyncConnection, entity_id: uuid.UUID, *, dry_run: bool, found: Run) -> None:
    stmt = sa.select(*_OLD).where(Entity.id == entity_id)
    if not dry_run:
        # FOR NO KEY UPDATE: a link written meanwhile takes only KEY SHARE on
        # its entity, so the write path is not held up.
        stmt = stmt.with_for_update(key_share=True)
    old = (await conn.execute(stmt)).one_or_none()
    if old is None:
        return
    names = _names(old.canonical_name, old.attributes)
    if not _conflicting(names):
        return
    groups = _groups(names)
    patterns = {key: _pattern(group) for key, group in groups.items()}
    canonical = _key(old.canonical_name)

    moves: dict[Key, list[tuple[uuid.UUID, datetime]]] = {}
    still_named: set[Key] = set()
    async for memory_id, content, created_at in _live_links(conn, entity_id, old.tenant_id):
        body = content.lower()
        named = {key for key, pattern in patterns.items() if pattern.search(body)}
        if len(named) == 1 and (key := next(iter(named))) != canonical:
            moves.setdefault(key, []).append((memory_id, created_at))
        else:
            still_named |= named
    if not moves:
        return

    found.split += 1
    for key, moved in moves.items():
        first_seen = min(created_at for _, created_at in moved)
        target = await _target(conn, old, groups[key], first_seen, dry_run=dry_run, found=found)
        found.moved += len(moved)
        if target is not None and not dry_run:
            await _move(conn, entity_id, target, old.tenant_id, [memory_id for memory_id, _ in moved])

    leaving = {name for key in moves if key not in still_named for name in groups[key]}
    if leaving and not dry_run:
        attributes = dict(old.attributes)
        attributes["_aliases"] = [alias for alias in attributes["_aliases"] if alias not in leaving]
        await conn.execute(sa.update(Entity).where(Entity.id == entity_id).values(attributes=attributes))


async def _run(*, tenant_id: str | None, batch: int, dry_run: bool, after_id: uuid.UUID | None = None) -> Run:
    logger.info(
        "%s; tenant=%s; after=%s",
        "dry run" if dry_run else "splitting",
        tenant_id or "all",
        after_id or "the first id",
    )
    found = Run(last_id=after_id)
    while True:
        async with get_engine().connect() as conn:
            rows = (await conn.execute(_walk(tenant_id=tenant_id, after=found.last_id, batch=batch))).all()
        if not rows:
            break
        for row in rows:
            if _conflicting(_names(row.canonical_name, row.attributes)):
                found.conflicting += 1
                async with get_engine().begin() as conn:
                    await _split(conn, row.id, dry_run=dry_run, found=found)
        found.entities += len(rows)
        found.last_id = rows[-1].id
        logger.info(
            "... %s entities, %s conflicting, %s split, %s links %s; resume with --after-id %s",
            found.entities,
            found.conflicting,
            found.split,
            found.moved,
            "to move" if dry_run else "moved",
            found.last_id,
        )
    logger.info(
        "done: %s entities with aliases, %s conflicting, %s split, %s links %s, %s new entities, %s existing",
        found.entities,
        found.conflicting,
        found.split,
        found.moved,
        "to move" if dry_run else "moved",
        found.new_entities,
        found.existing_entities,
    )
    return found


def main() -> None:
    p = argparse.ArgumentParser(prog="core_storage_api.scripts.backfill_split_merged_entities")
    p.add_argument("--tenant-id", default=None, help="only this tenant's entities")
    p.add_argument("--batch", type=int, default=1000, help="entities per read")
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
