"""H-05 (O2): the backfill splits entities the old nightly merge collapsed.

Before caura PR #1876 the nightly duplicate merge joined same-type entities by
name similarity alone, so 'Comet #0002' and 'Comet #0012' became one entity and
the second name survives only among its aliases.
``core_storage_api.scripts.backfill_split_merged_entities`` moves each live link
whose memory names one other name group, and no other, to an entity of that
name (owner decision 2026-10-13).

Each run here is scoped to its own tenant, as an operator can scope one, so the
backfill never touches another test's rows.
"""

from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select, text

from common.models import Entity, Memory, MemoryEntityLink
from core_storage_api.database.init import get_engine
from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("_ensure_schema")]

_svc = PostgresService()

JAN = datetime(2025, 1, 1, tzinfo=UTC)
JUN = datetime(2025, 6, 1, tzinfo=UTC)


def _backfill():
    """The script, imported when a test runs rather than at collection, so a tree
    without it fails these tests instead of stopping the whole suite."""
    return importlib.import_module("core_storage_api.scripts.backfill_split_merged_entities")


def _tenant() -> str:
    return f"h05-{uuid.uuid4().hex[:8]}"


async def _set(table: str, row_id: uuid.UUID, column: str, value: Any) -> None:
    async with get_engine().begin() as conn:
        await conn.execute(
            text(f"UPDATE {table} SET {column} = :v WHERE id = :id"), {"v": value, "id": row_id}
        )


async def _entity(tenant: str, name: str, *aliases: str) -> uuid.UUID:
    """An entity as a merge left it: both names among its aliases."""
    attributes = {"_aliases": sorted({name, *aliases})} if aliases else {}
    row = await _svc.entity_add(
        {"tenant_id": tenant, "entity_type": "project", "canonical_name": name, "attributes": attributes}
    )
    return row.id


async def _memory(tenant: str, text_: str, *, created_at: datetime = JAN, deleted: bool = False) -> uuid.UUID:
    row = await _svc.memory_add(
        {
            "tenant_id": tenant,
            "agent_id": "h05-agent",
            "content": f"{text_}. ref {uuid.uuid4()}",
            "memory_type": "fact",
            "status": "active",
            "visibility": "scope_team",
        }
    )
    await _set("memories", row.id, "created_at", created_at)
    if deleted:
        await _set("memories", row.id, "deleted_at", created_at)
    return row.id


async def _link(memory_id: uuid.UUID, entity_id: uuid.UUID) -> None:
    async with get_session() as session:
        session.add(MemoryEntityLink(memory_id=memory_id, entity_id=entity_id, role="mention"))


async def _links(memory_id: uuid.UUID) -> set[uuid.UUID]:
    """The entities ``memory_id`` links."""
    async with get_engine().connect() as conn:
        rows = await conn.execute(
            select(MemoryEntityLink.entity_id).where(MemoryEntityLink.memory_id == memory_id)
        )
        return set(rows.scalars())


async def _named(tenant: str, name: str) -> list:
    """The tenant's entities called ``name``, ignoring case."""
    async with get_engine().connect() as conn:
        rows = await conn.execute(
            select(Entity.id, Entity.canonical_name, Entity.entity_type, Entity.created_at).where(
                Entity.tenant_id == tenant, func.lower(Entity.canonical_name) == name.lower()
            )
        )
        return list(rows)


async def _aliases(entity_id: uuid.UUID) -> list[str]:
    async with get_engine().connect() as conn:
        result = await conn.execute(select(Entity.attributes).where(Entity.id == entity_id))
        return (result.scalar_one() or {}).get("_aliases", [])


async def _subject(memory_id: uuid.UUID) -> uuid.UUID | None:
    async with get_engine().connect() as conn:
        result = await conn.execute(select(Memory.subject_entity_id).where(Memory.id == memory_id))
        return result.scalar_one()


async def _run(tenant: str, *, dry_run: bool = False, after_id: uuid.UUID | None = None):
    return await _backfill()._run(tenant_id=tenant, batch=2, dry_run=dry_run, after_id=after_id)


async def test_a_link_whose_memory_names_another_group_moves_to_an_entity_of_that_name():
    tenant = _tenant()
    merged = await _entity(tenant, "Comet #0002", "Comet #0012")
    other = await _memory(tenant, "Launch notes for comet #0012")
    own = await _memory(tenant, "Comet #0002 shipped")
    plain = await _memory(tenant, "The comet team met")
    for memory in (other, own, plain):
        await _link(memory, merged)

    run = await _run(tenant)

    [split] = await _named(tenant, "comet #0012")
    assert (split.canonical_name, split.entity_type) == ("Comet #0012", "project")
    assert await _links(other) == {split.id}
    assert await _links(own) == await _links(plain) == {merged}
    assert "Comet #0012" not in await _aliases(merged)
    assert (run.conflicting, run.split, run.moved, run.new_entities) == (1, 1, 1, 1)


async def test_two_bracketed_qualifiers_split_too():
    tenant = _tenant()
    merged = await _entity(tenant, "Acme (Delaware)", "Acme (Ohio)")
    ohio = await _memory(tenant, "Acme (Ohio) signed the lease")
    await _link(ohio, merged)

    await _run(tenant)

    [split] = await _named(tenant, "acme (ohio)")
    assert await _links(ohio) == {split.id}


async def test_a_name_inside_a_longer_identifier_is_not_named():
    """'comet #12' and 'comet #1.5' do not name 'Comet #1'; a sentence ending
    in 'comet #1.' does."""
    tenant = _tenant()
    merged = await _entity(tenant, "Comet #2", "Comet #1")
    longer = await _memory(tenant, "comet #12 launched")
    dotted = await _memory(tenant, "comet #1.5 is the patch")
    ended = await _memory(tenant, "The launch was comet #1.")
    for memory in (longer, dotted, ended):
        await _link(memory, merged)

    await _run(tenant)

    [split] = await _named(tenant, "comet #1")
    assert await _links(longer) == await _links(dotted) == {merged}
    assert await _links(ended) == {split.id}


async def test_the_tenants_entity_of_that_name_takes_the_link():
    """The write path re-creates a split-off name on its next mention."""
    tenant = _tenant()
    merged = await _entity(tenant, "Comet #0002", "Comet #0012")
    existing = await _entity(tenant, "Comet #0012")
    moved = await _memory(tenant, "comet #0012 slipped a week")
    both = await _memory(tenant, "comet #0012 review")
    await _link(moved, merged)
    await _link(both, merged)
    await _link(both, existing)

    run = await _run(tenant)

    assert [e.id for e in await _named(tenant, "comet #0012")] == [existing]
    assert await _links(moved) == await _links(both) == {existing}
    assert (run.moved, run.new_entities, run.existing_entities) == (2, 0, 1)


async def test_a_link_naming_several_groups_stays_and_so_do_their_names():
    tenant = _tenant()
    merged = await _entity(tenant, "Comet #0002", "Comet #0012", "Comet #0013")
    one = await _memory(tenant, "comet #0012 is late")
    several = await _memory(tenant, "comet #0012 and comet #0013 share a pad")
    for memory in (one, several):
        await _link(memory, merged)

    first = await _run(tenant)
    again = await _run(tenant)

    [split] = await _named(tenant, "comet #0012")
    assert await _links(one) == {split.id}
    assert await _links(several) == {merged}
    assert {"Comet #0012", "Comet #0013"} <= set(await _aliases(merged))
    assert (first.moved, again.moved) == (1, 0)


async def test_a_moved_memorys_subject_moves_with_it():
    tenant = _tenant()
    merged = await _entity(tenant, "Comet #0002", "Comet #0012")
    moved = await _memory(tenant, "comet #0012 owns the launch")
    kept = await _memory(tenant, "comet #0002 owns the pad")
    for memory in (moved, kept):
        await _link(memory, merged)
        await _set("memories", memory, "subject_entity_id", merged)

    await _run(tenant)

    [split] = await _named(tenant, "comet #0012")
    assert await _subject(moved) == split.id
    assert await _subject(kept) == merged


async def test_a_new_entity_is_dated_from_its_earliest_moved_memory():
    tenant = _tenant()
    merged = await _entity(tenant, "Comet #0002", "Comet #0012")
    for when in (JUN, JAN):
        await _link(await _memory(tenant, "comet #0012 update", created_at=when), merged)

    await _run(tenant)

    [split] = await _named(tenant, "comet #0012")
    assert split.created_at == JAN


async def test_names_the_guard_lets_merge_are_left_alone():
    """'Acme' may join 'Acme (Ohio)', since an absent qualifier is unspecified,
    and 'Acme Corp' carries nothing to tell it apart."""
    tenant = _tenant()
    merged = await _entity(tenant, "Acme", "Acme (Ohio)", "Acme Corp")
    memory = await _memory(tenant, "Acme (Ohio) opened")
    await _link(memory, merged)

    run = await _run(tenant)

    assert await _links(memory) == {merged}
    assert await _named(tenant, "acme (ohio)") == []
    assert (run.entities, run.conflicting, run.moved) == (1, 0, 0)


async def test_only_a_live_memory_of_the_entitys_own_tenant_moves():
    tenant = _tenant()
    merged = await _entity(tenant, "Comet #0002", "Comet #0012")
    deleted = await _memory(tenant, "comet #0012 retired", deleted=True)
    foreign = await _memory(_tenant(), "comet #0012 elsewhere")
    for memory in (deleted, foreign):
        await _link(memory, merged)

    run = await _run(tenant)

    assert await _links(deleted) == await _links(foreign) == {merged}
    assert await _named(tenant, "comet #0012") == []
    assert (run.conflicting, run.moved) == (1, 0)


async def test_a_dry_run_counts_and_changes_nothing():
    tenant = _tenant()
    merged = await _entity(tenant, "Comet #0002", "Comet #0012", "Comet #0013")
    await _entity(tenant, "Comet #0013")
    to_new = await _memory(tenant, "comet #0012 first")
    to_existing = await _memory(tenant, "comet #0013 second")
    for memory in (to_new, to_existing):
        await _link(memory, merged)

    run = await _run(tenant, dry_run=True)

    assert (run.conflicting, run.split, run.moved, run.new_entities, run.existing_entities) == (1, 1, 2, 1, 1)
    assert await _links(to_new) == await _links(to_existing) == {merged}
    assert await _named(tenant, "comet #0012") == []
    assert set(await _aliases(merged)) == {"Comet #0002", "Comet #0012", "Comet #0013"}


async def test_a_run_scoped_to_one_tenant_leaves_the_others():
    tenant, other = _tenant(), _tenant()
    merged = await _entity(other, "Comet #0002", "Comet #0012")
    memory = await _memory(other, "comet #0012 elsewhere")
    await _link(memory, merged)

    await _run(tenant)

    assert await _links(memory) == {merged}


async def test_a_run_resumes_after_the_id_it_is_given():
    tenant = _tenant()
    comet = await _entity(tenant, "Comet #0002", "Comet #0012")
    nova = await _entity(tenant, "Nova #0001", "Nova #0009")
    named = {comet: await _memory(tenant, "comet #0012 log"), nova: await _memory(tenant, "nova #0009 log")}
    for entity, memory in named.items():
        await _link(memory, entity)
    first, second = sorted(named)

    run = await _run(tenant, after_id=first)

    assert await _links(named[first]) == {first}
    assert await _links(named[second]) != {second}
    assert run.last_id == second
