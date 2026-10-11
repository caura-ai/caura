"""M-112: the scored search must fetch each memory once, without the two columns
its route drops.

``memory_scored_search``'s outer query selected the whole ``Memory`` row and LEFT
JOINed its entity links, so a memory with k links came back in k rows, each
carrying its 1024-dim ``embedding`` (as text, parsed in Python) and its
``search_vector``. The route serialises ``MEMORY_LIST_FIELDS``, which leaves both
out.
"""

import contextlib
import uuid

import pytest
from sqlalchemy import Row, inspect

import core_storage_api.services.postgres_service as ps
from common.embedding import fake_embedding
from common.models import Entity, Memory, MemoryEntityLink
from tests.test_ann_pool_behavior import _SP, _insert

_CONTENT = "the harbour crane inspection moved to thursday"
_LINKS = 3


async def _seed_linked_memory(tenant_id: str) -> tuple[str, set[uuid.UUID]]:
    """One memory with ``_LINKS`` entity links."""
    memory_id = uuid.UUID(str((await _insert(tenant_id, _CONTENT))["id"]))
    entity_ids = {uuid.uuid4() for _ in range(_LINKS)}
    async with ps.get_session() as session:
        session.add_all(
            Entity(
                id=entity_id,
                tenant_id=tenant_id,
                entity_type="concept",
                canonical_name=f"m112 entity {entity_id}",
            )
            for entity_id in entity_ids
        )
        await session.flush()
        session.add_all(
            MemoryEntityLink(memory_id=memory_id, entity_id=entity_id, role="mentioned")
            for entity_id in entity_ids
        )
    return str(memory_id), entity_ids


async def _search(tenant_id: str) -> list:
    return await ps.PostgresService().memory_scored_search(
        tenant_id=tenant_id,
        embedding=fake_embedding(_CONTENT),
        query="harbour crane inspection",
        search_params=dict(_SP),
        top_k=5,
    )


def _record_fetched_memories(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The id of every ``Memory`` row the search's read session fetches."""
    fetched: list[str] = []
    real_get_read_session = ps.get_read_session

    @contextlib.asynccontextmanager
    async def _instrumented():
        async with real_get_read_session() as session:

            class _Spy:
                async def execute(self, stmt, *args, **kwargs):
                    frozen = (await session.execute(stmt, *args, **kwargs)).freeze()
                    # A one-entity select freezes to the entities themselves.
                    rows = (r if isinstance(r, Row) else (r,) for r in frozen.data)
                    fetched.extend(
                        str(value.id)
                        for row in rows
                        for value in row
                        if isinstance(value, Memory)
                    )
                    return frozen()

            yield _Spy()

    monkeypatch.setattr(ps, "get_read_session", _instrumented)
    return fetched


async def test_a_linked_memory_is_fetched_once(
    tenant_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_id, _ = await _seed_linked_memory(tenant_id)
    fetched = _record_fetched_memories(monkeypatch)

    [hit] = await _search(tenant_id)

    assert str(hit.Memory.id) == memory_id
    assert fetched == [memory_id], f"fetched once per entity link: {fetched}"


async def test_the_vector_and_the_tsvector_are_not_loaded(tenant_id: str) -> None:
    await _seed_linked_memory(tenant_id)

    [hit] = await _search(tenant_id)

    unloaded = inspect(hit.Memory).unloaded
    assert {"embedding", "search_vector"} <= unloaded, f"unloaded: {sorted(unloaded)}"


async def test_every_entity_link_still_comes_back(tenant_id: str) -> None:
    """Control: holds with and without the fix."""
    _, entity_ids = await _seed_linked_memory(tenant_id)

    [hit] = await _search(tenant_id)

    assert {link["entity_id"] for link in hit.entity_links} == entity_ids
    assert {link["role"] for link in hit.entity_links} == {"mentioned"}
