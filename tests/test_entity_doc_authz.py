"""Entity & document intra-tenant scope (parity with the memory by-id fixes).

- Entity side-door: get_entity returned ALL linked memories with full content,
  only checking the entity's tenant — leaking a peer agent's scope_agent secret
  / cross-fleet content by entity id. Now filtered via authorize_memory_access.
- Document delete: DELETE /documents/{id} (+ MCP op=delete) gated on write-cap
  only — a trust-1 agent could destroy tenant documents. Now enforce_delete.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest


def _mem(agent_id, visibility, fleet_id, content):
    return {
        "memory": {
            "id": str(uuid.uuid4()),
            "tenant_id": "t",
            "fleet_id": fleet_id,
            "agent_id": agent_id,
            "visibility": visibility,
            "memory_type": "fact",
            "content": content,
            "weight": 0.5,
            "source_uri": None,
            "run_id": None,
            "metadata": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "expires_at": None,
            "entity_links": [],
            "recall_count": 0,
            "last_recalled_at": None,
        }
    }


def _entity_result(linked):
    return {
        "entity": {
            "id": str(uuid.uuid4()),
            "tenant_id": "t",
            "fleet_id": "fleet-alpha",
            "entity_type": "person",
            "canonical_name": "X",
            "attributes": {},
            "relations": [],
        },
        "linked_memories": linked,
    }


@pytest.fixture
def fake_storage(monkeypatch):
    def _set(linked):
        sc = MagicMock()
        sc.get_entity_with_linked_memories = AsyncMock(
            return_value=_entity_result(linked)
        )
        from core_api.services import entity_service

        monkeypatch.setattr(entity_service, "get_storage_client", lambda: sc)

    return _set


@pytest.fixture
def patch_lookup(monkeypatch):
    def _set(*, fleet_id=None, trust_level=0, exists=True):
        from core_api.services import agent_service

        async def fake(tenant_id, agent_id):
            return (
                None
                if not exists
                else {
                    "agent_id": agent_id,
                    "fleet_id": fleet_id,
                    "trust_level": trust_level,
                }
            )

        monkeypatch.setattr(agent_service, "lookup_agent", fake)

    return _set


@pytest.mark.unit
async def test_entity_filters_scope_agent_and_cross_fleet(fake_storage, patch_lookup):
    from core_api.services.entity_service import get_entity

    fake_storage(
        [
            _mem(
                "alice", "scope_agent", "fleet-alpha", "alice private"
            ),  # peer's private → hide
            _mem(
                "alice", "scope_org", "fleet-beta", "org wide"
            ),  # tenant-global → keep
            _mem(
                "alice", "scope_team", "fleet-beta", "other fleet team"
            ),  # cross-fleet, trust<2 → hide
            _mem(
                "bob", "scope_agent", "fleet-alpha", "bob own private"
            ),  # caller's own → keep
        ]
    )
    patch_lookup(fleet_id="fleet-alpha", trust_level=1)  # bob: fleet-alpha, trust 1

    out = await get_entity(uuid.uuid4(), "t", caller_agent_id="bob")
    contents = {m.content for m in out.linked_memories}
    assert "alice private" not in contents
    assert "other fleet team" not in contents
    assert "org wide" in contents
    assert "bob own private" in contents


@pytest.mark.unit
async def test_entity_unfiltered_for_tenant_credential(fake_storage):
    """No agent identity (dashboard/admin) → full linked-memory reach, unchanged."""
    from core_api.services.entity_service import get_entity

    fake_storage([_mem("alice", "scope_agent", "fleet-beta", "alice private")])
    out = await get_entity(uuid.uuid4(), "t", caller_agent_id=None)
    assert "alice private" in {m.content for m in out.linked_memories}


# ── Document delete trust gate (API) ──


@pytest.fixture
def as_agent():
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id, agent_id):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id, agent_id=agent_id, readable_tenant_ids=[tenant_id]
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    from core_api.app import app as _app
    from core_api.auth import get_auth_context as _gac

    _app.dependency_overrides.pop(_gac, None)


@pytest.mark.integration
async def test_document_delete_blocked_for_low_trust_agent(
    client, as_agent, patch_lookup
):
    from tests.conftest import get_test_auth

    tenant_id, _ = get_test_auth()
    as_agent(tenant_id, "bob")
    patch_lookup(fleet_id="fleet-beta", trust_level=1)
    r = await client.delete(
        f"/api/v1/documents/some-doc?tenant_id={tenant_id}&collection=c"
    )
    assert r.status_code == 403, r.text

    # trust-3 passes the gate (404 because the doc doesn't exist, NOT 403)
    patch_lookup(fleet_id="fleet-beta", trust_level=3)
    r = await client.delete(
        f"/api/v1/documents/some-doc?tenant_id={tenant_id}&collection=c"
    )
    assert r.status_code != 403, r.text


@pytest.mark.integration
async def test_document_delete_tenant_key_unchanged(client):
    from tests.conftest import get_test_auth

    tenant_id, headers = get_test_auth()
    r = await client.delete(
        f"/api/v1/documents/nope-{uuid.uuid4().hex[:6]}?tenant_id={tenant_id}&collection=c",
        headers=headers,
    )
    assert r.status_code != 403, r.text
