"""M-83: an entity hidden from an agent stays hidden when named by id or name.

An entity is hidden from an agent's lists, search and graph when every memory
behind it is one the agent may not read. Two doors still showed it (owner
decision 2026-10-05):

- ``GET /entities/{id}`` and MCP ``caura_entity_get`` returned the entity's
  name and attributes, with only the unreadable memories filtered out. They
  now answer as for a missing id.
- ``POST /entities/upsert`` of a guessed name merged into the hidden entity
  and answered with its stored attributes and ``_aliases``. It still merges,
  and answers with only the fields the caller sent.

Real storage through the in-process bridge, so the visibility predicate that
decides is the one the lists use.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from core_api import mcp_server
from core_api.app import app
from core_api.auth import AuthContext, get_auth_context
from core_api.tenant_context import set_current_tenant
from tests._mcp_test_helpers import as_text, is_error_envelope, parse_envelope
from tests.conftest import new_tenant_id

pytestmark = pytest.mark.asyncio

NAME = "zenith acquisition"


@pytest.fixture
def as_auth():
    def _install(tenant: str, agent_id: str | None = None) -> None:
        async def _dep():
            set_current_tenant(tenant)
            return AuthContext(
                tenant_id=tenant, agent_id=agent_id, readable_tenant_ids=[tenant]
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


async def _entity(sc, tenant: str, *, behind: str | None) -> str:
    """An entity carrying a stored attribute, mined from ``owner``'s note of
    visibility ``behind``, or from no memory when ``behind`` is None."""
    entity = await sc.create_entity(
        {
            "tenant_id": tenant,
            "entity_type": "concept",
            "canonical_name": NAME,
            "attributes": {"price": "4.2B"},
        }
    )
    if behind:
        memory = await sc.create_memory(
            {
                "tenant_id": tenant,
                "agent_id": "owner",
                "memory_type": "fact",
                "content": "Zenith acquisition closes at 4.2B.",
                "status": "active",
                "visibility": behind,
            }
        )
        await sc.create_entity_link(
            tenant,
            {
                "memory_id": str(memory["id"]),
                "entity_id": str(entity["id"]),
                "role": "subject",
            },
        )
    return str(entity["id"])


async def _get(client, tenant: str, entity: str):
    return await client.get(f"/api/v1/entities/{entity}", params={"tenant_id": tenant})


async def test_an_agent_reads_a_hidden_entity_as_missing(client, sc, as_auth):
    tenant = new_tenant_id()
    entity = await _entity(sc, tenant, behind="scope_agent")
    as_auth(tenant, agent_id="peer")

    resp = await _get(client, tenant, entity)

    assert resp.status_code == 404, resp.text


async def test_its_author_and_a_tenant_credential_still_read_it(client, sc, as_auth):
    tenant = new_tenant_id()
    entity = await _entity(sc, tenant, behind="scope_agent")

    as_auth(tenant, agent_id="owner")
    own = await _get(client, tenant, entity)
    as_auth(tenant)
    tenant_wide = await _get(client, tenant, entity)

    assert own.status_code == 200, own.text
    assert len(own.json()["linked_memories"]) == 1
    assert tenant_wide.status_code == 200, tenant_wide.text


async def test_mcp_entity_get_reads_a_hidden_entity_as_missing(sc, monkeypatch):
    tenant = new_tenant_id()
    entity = await _entity(sc, tenant, behind="scope_agent")
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: None)
    monkeypatch.setattr(mcp_server, "_get_tenant", lambda: tenant)
    monkeypatch.setattr(
        mcp_server, "_get_agent_id", lambda: mcp_server.AgentIdentity("peer")
    )

    hidden = await mcp_server.caura_entity_get(entity_id=entity)
    missing = await mcp_server.caura_entity_get(entity_id=str(uuid4()))

    # M-23: NOT_FOUND in the canonical envelope, with isError, as REST's 404.
    # It was the prose "Entity not found." with a latency trailer.
    assert is_error_envelope(hidden), as_text(hidden)
    assert parse_envelope(hidden) == parse_envelope(missing)
    assert parse_envelope(hidden)["error"]["code"] == "NOT_FOUND"


async def _upsert(client, tenant: str):
    return await client.post(
        "/api/v1/entities/upsert",
        json={
            "tenant_id": tenant,
            "entity_type": "concept",
            "canonical_name": NAME,
            "attributes": {"note": "seen in a filing"},
        },
    )


async def test_an_upsert_onto_a_hidden_entity_answers_with_what_it_sent(
    client, sc, as_auth
):
    tenant = new_tenant_id()
    entity = await _entity(sc, tenant, behind="scope_agent")
    as_auth(tenant, agent_id="peer")

    resp = await _upsert(client, tenant)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == entity
    assert body["canonical_name"] == NAME
    assert body["attributes"] == {"note": "seen in a filing"}
    # Still merged, as decided: the write lands, only the answer is narrowed.
    stored = (await sc.get_entity(entity, tenant))["attributes"]
    assert stored["price"] == "4.2B"
    assert stored["note"] == "seen in a filing"


async def test_an_upsert_onto_a_visible_entity_answers_with_the_merged_row(
    client, sc, as_auth
):
    tenant = new_tenant_id()
    await _entity(sc, tenant, behind=None)
    as_auth(tenant, agent_id="peer")

    resp = await _upsert(client, tenant)

    assert resp.status_code == 200, resp.text
    attributes = resp.json()["attributes"]
    assert attributes["price"] == "4.2B"
    assert attributes["note"] == "seen in a filing"


async def test_the_check_after_an_upsert_reads_the_writer(
    client, sc, as_auth, monkeypatch
):
    """The visibility check reads back the row the upsert just wrote, so a
    replica that has not caught up must not narrow a visible entity's answer."""
    tenant = new_tenant_id()
    entity = await _entity(sc, tenant, behind=None)
    as_auth(tenant, agent_id="peer")
    real_get = sc._get

    async def _lagging_replica(path, *, read=True, **params):
        if read and path == f"/entities/{entity}":
            return None
        return await real_get(path, read=read, **params)

    monkeypatch.setattr(sc, "_get", _lagging_replica)

    resp = await _upsert(client, tenant)

    assert resp.status_code == 200, resp.text
    attributes = resp.json()["attributes"]
    assert attributes.get("price") == "4.2B", attributes
    assert attributes["note"] == "seen in a filing"
