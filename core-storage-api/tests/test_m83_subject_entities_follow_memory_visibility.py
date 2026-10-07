"""M-83: an entity that is only a memory's subject follows that memory's visibility.

Entity listing hides an entity whose memories the reader may not read, judged
over ``memory_entity_links``. A memory can also name an entity with no link:
``memories.subject_entity_id``, which the write path sets for an identifier
subject (``CreatePendingSubject``) without writing a link. Extraction, which
might add one, is off when no provider is set. Such an entity counted as having
no memory behind it, and those are listed for every agent, so ``ZEN-ACQ-7``
from a peer's private "ZEN-ACQ-7 status is signed" stayed listed and on the
graph. The subject pointer now counts as a memory behind the entity.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_entity_reader_scope import _entity
from tests.test_integration import PREFIX, _memory_payload

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _subject_of(
    client: AsyncClient, tenant: str, entity_id: str, *, agent: str, visibility: str
) -> None:
    """A memory naming ``entity_id`` as its subject, with no link to it."""
    payload = _memory_payload(tenant, "fleet-x")
    payload.update(agent_id=agent, visibility=visibility, subject_entity_id=entity_id)
    resp = await client.post(f"{PREFIX}/memories", json=payload)
    assert resp.status_code == 200, resp.text


async def test_an_entity_named_only_as_a_private_subject_is_hidden_from_peers(client: AsyncClient) -> None:
    tenant = f"test-tenant-{uuid.uuid4().hex[:8]}"
    secret = await _entity(client, tenant, f"ZEN-ACQ-{uuid.uuid4().hex[:6]}")
    await _subject_of(client, tenant, secret, agent="b", visibility="scope_agent")
    shared = await _entity(client, tenant, f"OPS-{uuid.uuid4().hex[:6]}")
    await _subject_of(client, tenant, shared, agent="b", visibility="scope_team")
    # Control: no memory behind it at all, so nothing private to hide.
    manual = await _entity(client, tenant, f"hand built {uuid.uuid4().hex[:6]}")

    async def listed(**reader) -> set[str]:
        resp = await client.get(f"{PREFIX}/entities", params={"tenant_id": tenant, **reader})
        assert resp.status_code == 200, resp.text
        return {e["id"] for e in resp.json()}

    async def graph_nodes(**reader) -> set[str]:
        resp = await client.get(f"{PREFIX}/entities/full-graph", params={"tenant_id": tenant, **reader})
        assert resp.status_code == 200, resp.text
        return {e["id"] for e in resp.json()["entities"]}

    for read in (listed, graph_nodes):
        assert await read(caller_agent_id="a") == {shared, manual}, read.__name__
        # The author, and a tenant credential, still see it.
        assert await read(caller_agent_id="b") == {secret, shared, manual}, read.__name__
        assert await read() == {secret, shared, manual}, read.__name__
