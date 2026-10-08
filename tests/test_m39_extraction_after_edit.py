"""M-39: extraction never leaves a graph for text the memory no longer holds.

Every write and every content edit schedules ``process_entity_extraction``
fire-and-forget, with the content passed by value, and the LLM call takes
seconds. An edit inside that window clears ``subject_entity_id`` and resets the
memory's extraction rows while the old run has written nothing yet. The old run
then wrote links, relations, a subject and a predicate for the old text. Its
only re-checks compared ``deleted_at``, and the edit's own re-extraction, being
``IS NULL``-guarded, kept the stale subject.

The worker now compares the stored content with the text it extracted from. A
mismatch before it writes discards the run. A mismatch after it wrote resets
the memory's extraction rows and re-extracts the current text. The subject and
predicate write-backs land only while the row still holds the extracted text.

Real storage throughout, through the in-process bridge, because each claim is
about which rows survive. ``_edit`` does to the row what ``update_memory`` does
on a content edit, so each test can land it at one point of the run.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from core_api.services import entity_extraction_worker as w
from core_api.services.entity_extraction import (
    ExtractedEntity,
    ExtractedGraph,
    ExtractedRelation,
)
from tests.conftest import close_scheduled_coro

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

OLD = "Alice Morgan is located in the Berlin office."
NEW = "Bob Carter is located in the Lisbon office."
_NAMES = {
    OLD: ("alice morgan", "berlin office"),
    NEW: ("bob carter", "lisbon office"),
}
_CONFIG = SimpleNamespace(
    entity_blocklist=frozenset(), auto_entity_linking_enabled=False
)


def _graph(content: str) -> ExtractedGraph:
    person, place = _NAMES[content]
    return ExtractedGraph(
        entities=[
            ExtractedEntity(
                canonical_name=person, entity_type="person", role="subject"
            ),
            ExtractedEntity(canonical_name=place, entity_type="location"),
        ],
        relations=[
            ExtractedRelation(
                from_entity=person, relation_type="located_in", to_entity=place
            )
        ],
    )


async def _memory(sc, tenant: str, content: str = OLD) -> str:
    row = await sc.create_memory(
        {
            "tenant_id": tenant,
            "agent_id": "m39-agent",
            "memory_type": "fact",
            "content": content,
            "status": "active",
            "visibility": "scope_team",
        }
    )
    return str(row["id"])


async def _edit(sc, tenant: str, memory_id: str) -> None:
    """``update_memory`` on a content change: the patch, then the reset."""
    await sc.update_memory(
        memory_id, tenant, {"content": NEW, "subject_entity_id": None}
    )
    await sc.reset_entity_artifacts(tenant, memory_id)


async def _extract(
    memory_id: str,
    tenant: str,
    *,
    on_extract: Callable[[], Awaitable[None]] | None = None,
) -> list[str]:
    """Run the worker on ``OLD``; returns the texts the extractor was given."""
    seen: list[str] = []

    async def extract(content, memory_type, tenant_config=None, **_kw):
        seen.append(content)
        if on_extract is not None and len(seen) == 1:
            await on_extract()
        return _graph(content)

    with (
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=_CONFIG),
        ),
        patch.object(w, "extract_entities_from_content", new=extract),
        patch.object(w, "get_embedding", new=AsyncMock(return_value=None)),
        patch.object(w, "log_action", new=AsyncMock()),
        patch("core_api.tasks.track_task", side_effect=close_scheduled_coro),
    ):
        await w.process_entity_extraction(
            UUID(memory_id), tenant, None, "m39-agent", OLD, "fact"
        )
    return seen


async def _graph_state(sc, tenant: str, memory_id: str) -> dict:
    names = {e["id"]: e["canonical_name"] for e in await sc.list_entities(tenant)}
    links = await sc.get_entity_links_for_memories([memory_id], tenant)
    row = await sc.get_memory(memory_id, tenant, read=False)
    subject = row["subject_entity_id"]
    return {
        "linked": {names[link["entity_id"]] for link in links.get(memory_id, [])},
        "subject": names.get(subject) if subject else None,
        "object_value": row["object_value"],
        "entities": set(names.values()),
    }


def _tenant() -> str:
    return f"test-m39-{uuid4().hex[:8]}"


async def test_an_unedited_memory_is_extracted_as_before(sc):
    """The control: the content check does not reject a row it should accept."""
    tenant = _tenant()
    memory_id = await _memory(sc, tenant)

    assert await _extract(memory_id, tenant) == [OLD]

    state = await _graph_state(sc, tenant, memory_id)
    assert state["linked"] == {"alice morgan", "berlin office"}
    assert state["subject"] == "alice morgan"
    assert state["object_value"] == "berlin office"


async def test_an_edit_during_the_llm_call_writes_nothing_for_the_old_text(sc):
    """Nothing is written yet, so the run stops; the edit's own extraction
    covers the new text."""
    tenant = _tenant()
    memory_id = await _memory(sc, tenant)

    seen = await _extract(
        memory_id, tenant, on_extract=lambda: _edit(sc, tenant, memory_id)
    )

    assert seen == [OLD]
    state = await _graph_state(sc, tenant, memory_id)
    assert state["linked"] == set()
    assert state["subject"] is None
    assert state["object_value"] is None
    assert state["entities"] == set()


async def test_an_edit_during_the_writes_is_reset_and_re_extracted(sc):
    """The edit lands after the pre-write check: the old text's links land,
    the check after them sees the new text, resets and re-extracts it."""
    tenant = _tenant()
    memory_id = await _memory(sc, tenant)
    resolve = sc.bulk_resolve_entities
    edited: list[bool] = []

    async def edit_then_resolve(*args, **kwargs):
        if not edited:
            edited.append(True)
            await _edit(sc, tenant, memory_id)
        return await resolve(*args, **kwargs)

    with patch.object(sc, "bulk_resolve_entities", new=edit_then_resolve):
        seen = await _extract(memory_id, tenant)

    assert seen == [OLD, NEW]
    state = await _graph_state(sc, tenant, memory_id)
    assert state["linked"] == {"bob carter", "lisbon office"}
    assert state["subject"] == "bob carter"
    assert state["object_value"] == "lisbon office"
    # Orphaned by the reset, so gone: nothing names the old text any more.
    assert not state["entities"] & {"alice morgan", "berlin office"}


async def test_a_stale_subject_and_relation_never_survive_an_edit(sc):
    """The edit lands after the check that follows the links, so the subject,
    relation and predicate write-backs all run for the old text. The old
    entities are shared with another memory, so the edit's reset keeps them."""
    tenant = _tenant()
    memory_id = await _memory(sc, tenant)
    other_id = await _memory(sc, tenant, content="The Berlin office hired Alice.")
    shared = [
        await sc.create_entity(
            {
                "tenant_id": tenant,
                "entity_type": entity_type,
                "canonical_name": name,
                "attributes": {},
            }
        )
        for name, entity_type in (
            ("alice morgan", "person"),
            ("berlin office", "location"),
        )
    ]
    await sc.bulk_upsert_entity_links(
        tenant,
        items=[
            {
                "input_idx": i,
                "memory_id": other_id,
                "entity_id": str(e["id"]),
                "role": "mentioned",
            }
            for i, e in enumerate(shared)
        ],
    )
    get_memory = sc.get_memory
    reads: list[bool] = []

    async def read_then_edit(*args, **kwargs):
        row = await get_memory(*args, **kwargs)
        reads.append(True)
        if len(reads) == 2:  # the check right after the link upsert
            await _edit(sc, tenant, memory_id)
        return row

    with patch.object(sc, "get_memory", new=read_then_edit):
        seen = await _extract(memory_id, tenant)

    assert seen == [OLD, NEW]
    state = await _graph_state(sc, tenant, memory_id)
    assert state["subject"] == "bob carter"
    assert state["object_value"] == "lisbon office"
    assert state["linked"] == {"bob carter", "lisbon office"}
    relations = await sc.get_outgoing_relations(str(shared[0]["id"]), tenant)
    assert [r for r in relations if r["evidence_memory_id"] == memory_id] == []


async def test_the_subject_write_back_lands_only_on_the_text_it_came_from(sc):
    tenant = _tenant()
    memory_id = await _memory(sc, tenant)
    entity = await sc.create_entity(
        {
            "tenant_id": tenant,
            "entity_type": "person",
            "canonical_name": "alice morgan",
            "attributes": {},
        }
    )
    eid = str(entity["id"])

    assert not await sc.set_subject_entity_if_null(memory_id, tenant, eid, content=NEW)
    row = await sc.get_memory(memory_id, tenant, read=False)
    assert row["subject_entity_id"] is None

    assert await sc.set_subject_entity_if_null(memory_id, tenant, eid, content=OLD)
    row = await sc.get_memory(memory_id, tenant, read=False)
    assert row["subject_entity_id"] == eid


async def test_the_predicate_write_back_lands_only_on_the_text_it_came_from(sc):
    tenant = _tenant()
    memory_id = await _memory(sc, tenant)

    assert not await sc.set_predicate_if_null(
        memory_id, tenant, "located_in", "berlin office", content=NEW
    )
    row = await sc.get_memory(memory_id, tenant, read=False)
    assert row["predicate"] is None

    assert await sc.set_predicate_if_null(
        memory_id, tenant, "located_in", "berlin office", content=OLD
    )
    row = await sc.get_memory(memory_id, tenant, read=False)
    assert (row["predicate"], row["object_value"]) == ("located_in", "berlin office")
