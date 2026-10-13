"""O13: the backfill marks text-mined cross-links as extraction's (M-65, migration 048).

``entity_discover_cross_links`` wrote no ``source`` until caura PR #1776, so the
cross-links it mined read ``caller`` and outlived every content edit.
``core_storage_api.scripts.backfill_048_cross_link_source`` marks them
``extraction`` by discovery's own test: a ``caller`` link with role
``mentioned`` whose entity's name or an alias is in its memory's text (owner
decision 2026-10-13). A link the text does not name stays ``caller``.

Each run here is scoped to its own tenant, as an operator can scope one, so the
backfill never touches another test's rows.
"""

from __future__ import annotations

import importlib
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

from common.models import MemoryEntityLink
from core_storage_api.database.init import get_engine
from core_storage_api.services.postgres_service import PostgresService, get_session

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("_ensure_schema")]

_svc = PostgresService()


def _backfill():
    """The script, imported when a test runs rather than at collection, so a tree
    without it fails these tests instead of stopping the whole suite."""
    return importlib.import_module("core_storage_api.scripts.backfill_048_cross_link_source")


def _tenant() -> str:
    return f"o13-{uuid.uuid4().hex[:8]}"


async def _entity(tenant: str, name: str, *, aliases: list[str] | None = None) -> uuid.UUID:
    data: dict = {"tenant_id": tenant, "entity_type": "organization", "canonical_name": name}
    if aliases is not None:
        data["attributes"] = {"_aliases": aliases}
    return (await _svc.entity_add(data)).id


async def _memory(tenant: str, content: str, *, deleted: bool = False) -> uuid.UUID:
    row = await _svc.memory_add(
        {
            "tenant_id": tenant,
            "agent_id": "o13-agent",
            "content": content,
            "memory_type": "fact",
            "status": "active",
            "visibility": "scope_team",
        }
    )
    if deleted:
        async with get_engine().begin() as conn:
            await conn.execute(
                text("UPDATE memories SET deleted_at = :at WHERE id = :id"),
                {"at": datetime(2026, 10, 1, tzinfo=UTC), "id": row.id},
            )
    return row.id


async def _link(
    memory_id: uuid.UUID, entity_id: uuid.UUID, *, role: str = "mentioned", source: str = "caller"
) -> None:
    async with get_session() as session:
        session.add(MemoryEntityLink(memory_id=memory_id, entity_id=entity_id, role=role, source=source))


async def _source(memory_id: uuid.UUID, entity_id: uuid.UUID) -> str | None:
    async with get_engine().connect() as conn:
        return (
            await conn.execute(
                select(MemoryEntityLink.source).where(
                    MemoryEntityLink.memory_id == memory_id, MemoryEntityLink.entity_id == entity_id
                )
            )
        ).scalar_one_or_none()


async def _run(tenant: str, *, apply: bool = True, after_id: uuid.UUID | None = None):
    return await _backfill()._run(tenant_id=tenant, batch=2, apply=apply, after_id=after_id)


@pytest.mark.parametrize(
    ("content", "name", "aliases"),
    [
        ("Alice joined Acme Corp last week.", "Acme Corp", None),
        ("ACME CORP signed the renewal.", "Acme Corp", None),
        ("We bought an IBM server.", "International Business Machines", ["IBM"]),
    ],
)
async def test_a_link_its_text_names_becomes_extractions(content, name, aliases):
    tenant = _tenant()
    memory = await _memory(tenant, content)
    entity = await _entity(tenant, name, aliases=aliases)
    await _link(memory, entity)

    run = await _run(tenant)

    assert await _source(memory, entity) == "extraction"
    assert (run.candidates, run.marked) == (1, 1)


async def test_a_link_its_text_never_names_stays_callers():
    """A caller's tag for an entity its text never names, which 048 protects."""
    tenant = _tenant()
    memory = await _memory(tenant, "Quarterly planning notes for the platform team.")
    entity = await _entity(tenant, "Project Falcon")
    await _link(memory, entity)

    run = await _run(tenant)

    assert await _source(memory, entity) == "caller"
    assert (run.candidates, run.marked) == (1, 0)


async def test_a_link_with_another_role_stays_callers():
    """Discovery writes only ``mentioned``."""
    tenant = _tenant()
    memory = await _memory(tenant, "Alice joined Acme Corp last week.")
    entity = await _entity(tenant, "Acme Corp")
    await _link(memory, entity, role="subject")

    run = await _run(tenant)

    assert await _source(memory, entity) == "caller"
    assert run.candidates == 0


async def test_an_empty_alias_names_nothing():
    tenant = _tenant()
    memory = await _memory(tenant, "Quarterly planning notes for the platform team.")
    entity = await _entity(tenant, "Project Falcon", aliases=["", "  "])
    await _link(memory, entity)

    await _run(tenant)

    assert await _source(memory, entity) == "caller"


async def test_a_deleted_memorys_links_stay():
    tenant = _tenant()
    memory = await _memory(tenant, "Alice joined Acme Corp last week.", deleted=True)
    entity = await _entity(tenant, "Acme Corp")
    await _link(memory, entity)

    await _run(tenant)

    assert await _source(memory, entity) == "caller"


async def test_an_entity_of_another_tenant_stays():
    tenant = _tenant()
    memory = await _memory(tenant, "Alice joined Acme Corp last week.")
    entity = await _entity(_tenant(), "Acme Corp")
    await _link(memory, entity)

    await _run(tenant)

    assert await _source(memory, entity) == "caller"


async def test_a_dry_run_counts_and_changes_nothing():
    tenant = _tenant()
    memory = await _memory(tenant, "Alice joined Acme Corp last week.")
    entity = await _entity(tenant, "Acme Corp")
    await _link(memory, entity)

    run = await _run(tenant, apply=False)

    assert (run.memories, run.candidates, run.marked) == (1, 1, 1)
    assert await _source(memory, entity) == "caller"


async def test_the_command_line_is_a_dry_run_unless_told_to_apply():
    assert _backfill()._args(["--tenant-id", "t"]).apply is False
    assert _backfill()._args(["--tenant-id", "t", "--apply"]).apply is True


async def test_a_run_scoped_to_one_tenant_leaves_the_others():
    tenant, other = _tenant(), _tenant()
    memory = await _memory(other, "Alice joined Acme Corp last week.")
    entity = await _entity(other, "Acme Corp")
    await _link(memory, entity)

    await _run(tenant)

    assert await _source(memory, entity) == "caller"


async def test_a_second_run_finds_nothing_to_mark():
    tenant = _tenant()
    memory = await _memory(tenant, "Alice joined Acme Corp last week.")
    entity = await _entity(tenant, "Acme Corp")
    await _link(memory, entity)
    await _run(tenant)

    again = await _run(tenant)

    assert (again.memories, again.candidates, again.marked) == (1, 0, 0)


async def test_a_run_resumes_after_the_id_it_is_given():
    tenant = _tenant()
    acme = await _entity(tenant, "Acme Corp")
    first, second = sorted(
        [await _memory(tenant, "Acme Corp renewed."), await _memory(tenant, "Acme Corp churned.")]
    )
    for memory in (first, second):
        await _link(memory, acme)

    run = await _run(tenant, after_id=first)

    assert await _source(first, acme) == "caller"
    assert await _source(second, acme) == "extraction"
    assert run.last_id == second


@pytest.mark.parametrize("backfilled", [True, False])
async def test_the_next_content_edit_clears_a_marked_link_and_keeps_a_callers_tag(backfilled):
    """What the backfill is for. Control: without it, the reset keeps both."""
    tenant = _tenant()
    memory = await _memory(tenant, "Alice joined Acme Corp last week.")
    acme = await _entity(tenant, "Acme Corp")
    falcon = await _entity(tenant, "Project Falcon")
    await _link(memory, acme)
    await _link(memory, falcon, role="project")
    if backfilled:
        await _run(tenant)

    await _svc.memory_reset_entity_artifacts(tenant, memory)

    assert await _source(memory, acme) == (None if backfilled else "caller")
    assert await _source(memory, falcon) == "caller"
