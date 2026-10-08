"""Each request reads its caller's agent row once (audit 2026-10-01, B33).

L-178: search, recall and every write read the caller's agent row, and then
their fleet gate read it again: ``enforce_fleet_read_many`` and
``enforce_fleet_write`` each looked the agent up for two fields the route
already held. A broker write read it a third time, in its ownership gate. The
gates now take the row the route holds, and a broker write registers the row
its gate read.

L-177: the REST keystone upsert looked its caller's trust up twice, before and
after reading the stored rule. It now reads it once and holds it to the rule's
floor, as the delete route and ``caura_keystones_set`` already did.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from core_api import mcp_server
from core_api.auth import AuthContext
from core_api.clients.storage_client import CoreStorageClient
from core_api.routes import keystones
from core_api.routes import memories as memories_route
from core_api.services import agent_service
from tests._mcp_test_helpers import is_error_envelope
from tests.conftest import get_test_auth
from tests.conftest import uid as _uid


def _stop(detail: str) -> AsyncMock:
    """A stand-in that ends the request with a 418 once the gates are behind it."""
    return AsyncMock(side_effect=HTTPException(status_code=418, detail=detail))


@pytest.fixture
def as_agent():
    """Authenticate as an agent-scoped credential, as test_h06_m30 does."""
    from core_api.app import app
    from core_api.auth import get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str, agent_id: str) -> None:
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id,
                agent_id=agent_id,
                readable_tenant_ids=[tenant_id],
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


@pytest.fixture
def agent_reads(monkeypatch):
    """The agent id of every storage ``get_agent`` call, in order."""
    reads: list[str] = []
    real = CoreStorageClient.get_agent

    async def _counted(self, agent_id, tenant_id, *, read=True):
        reads.append(agent_id)
        return await real(self, agent_id, tenant_id, read=read)

    monkeypatch.setattr(CoreStorageClient, "get_agent", _counted)
    return reads


async def _seed(sc, tenant_id: str) -> tuple[str, str]:
    """A trust-1 agent with a home fleet."""
    agent_id, fleet_id = f"reads-once-{_uid()}", f"fleet-{_uid()}"
    await sc.create_or_update_agent(
        {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "trust_level": 1,
        }
    )
    return agent_id, fleet_id


class _Out:
    def model_dump(self, mode: str = "python") -> dict:
        return {"id": "m-1", "status": "created"}


# --- L-178: REST reads and writes --------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("path", ["/api/v1/search", "/api/v1/recall"])
@pytest.mark.parametrize("named", [True, False], ids=["fleet-named", "fleet-forced"])
async def test_l178_a_read_looks_its_caller_up_once(
    client, sc, as_agent, agent_reads, monkeypatch, path, named
):
    tenant_id, _ = get_test_auth()
    agent_id, fleet_id = await _seed(sc, tenant_id)
    as_agent(tenant_id, agent_id)
    # The meter is the first step after the fleet gate.
    monkeypatch.setattr(memories_route, "check_and_increment", _stop("metered"))
    agent_reads.clear()

    body: dict = {"tenant_id": tenant_id, "query": "what the fleet knows"}
    if named:
        body["fleet_ids"] = [fleet_id]
    resp = await client.post(path, json=body)

    assert resp.status_code == 418, resp.text  # its own fleet passed the gate
    assert agent_reads.count(agent_id) == 1


@pytest.mark.integration
async def test_l178_a_write_looks_its_agent_up_once(
    client, sc, as_agent, agent_reads, monkeypatch
):
    tenant_id, _ = get_test_auth()
    agent_id, fleet_id = await _seed(sc, tenant_id)
    as_agent(tenant_id, agent_id)
    # The write itself comes after registration and the fleet gate.
    monkeypatch.setattr(memories_route, "create_memory", _stop("written"))
    agent_reads.clear()

    resp = await client.post(
        "/api/v1/memories",
        json={"tenant_id": tenant_id, "content": "a fact", "fleet_id": fleet_id},
    )

    assert resp.status_code == 418, resp.text
    assert agent_reads.count(agent_id) == 1


# --- L-178: the broker ownership gate and MCP ---------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("owner", ["install-1", None], ids=["owned", "unclaimed"])
async def test_l178_a_broker_write_registers_the_row_its_gate_read(monkeypatch, owner):
    row = {
        "agent_id": "mine",
        "fleet_id": "f1",
        "trust_level": 1,
        "owner_install_uuid": owner,
    }
    storage = SimpleNamespace(
        get_agent=AsyncMock(return_value=row), create_or_update_agent=AsyncMock()
    )
    monkeypatch.setattr(agent_service, "get_storage_client", lambda: storage)

    agent, agent_id = await agent_service.resolve_write_agent(
        "mine",
        "t-reads-once",
        "f1",
        is_install_credential=True,
        install_uuid="install-1",
    )

    assert agent_id == "mine"
    storage.get_agent.assert_awaited_once()
    # An unclaimed agent is still stamped as this install's, and only then.
    assert agent["owner_install_uuid"] == "install-1"
    assert storage.create_or_update_agent.await_count == (0 if owner else 1)


@pytest.mark.unit
async def test_l178_mcp_recall_looks_its_caller_up_once(mcp_env, monkeypatch):
    row = {"agent_id": "a1", "fleet_id": "f1", "trust_level": 1}
    storage = SimpleNamespace(get_agent=AsyncMock(return_value=row))
    monkeypatch.setattr(agent_service, "get_storage_client", lambda: storage)
    mcp_server.check_and_increment.side_effect = HTTPException(
        status_code=418, detail="metered"
    )

    out = await mcp_server.caura_recall(query="q", agent_id="a1", fleet_ids=["f1"])

    assert is_error_envelope(out)
    mcp_server.check_and_increment.assert_awaited_once()  # past the fleet gate
    storage.get_agent.assert_awaited_once()


@pytest.mark.unit
async def test_l178_mcp_write_looks_its_agent_up_once(mcp_env, monkeypatch):
    # ``mcp_env`` stubs both; this is about the real pair.
    monkeypatch.setattr(
        mcp_server, "resolve_write_agent", agent_service.resolve_write_agent
    )
    monkeypatch.setattr(
        mcp_server, "enforce_fleet_write", agent_service.enforce_fleet_write
    )
    row = {"agent_id": "a1", "fleet_id": "f1", "trust_level": 1}
    storage = SimpleNamespace(get_agent=AsyncMock(return_value=row))
    monkeypatch.setattr(agent_service, "get_storage_client", lambda: storage)
    written = mcp_env["service"]("create_memory")
    written.return_value = _Out()

    await mcp_server.caura_write(content="a fact", agent_id="a1", fleet_id="f1")

    written.assert_awaited_once()
    storage.get_agent.assert_awaited_once()


# --- L-177: the keystone upsert -----------------------------------------------------


def _keystone_storage(monkeypatch, *, trust: int, stored: dict | None):
    """One stub behind the trust lookup and the route's own storage calls."""
    storage = SimpleNamespace(
        get_agent=AsyncMock(return_value={"agent_id": "author", "trust_level": trust}),
        get_document=AsyncMock(return_value=stored),
        upsert_keystone=AsyncMock(return_value={"id": "k-1"}),
    )
    monkeypatch.setattr(agent_service, "get_storage_client", lambda: storage)
    monkeypatch.setattr(keystones, "get_storage_client", lambda: storage)
    monkeypatch.setattr(keystones, "log_action", AsyncMock())
    return storage


def _author() -> AuthContext:
    return AuthContext(
        tenant_id="t-reads-once", agent_id="author", agent_id_verified=True
    )


def _rule(**fields) -> keystones.KeystoneSetRequest:
    return keystones.KeystoneSetRequest(
        tenant_id="t-reads-once",
        doc_id="no-secrets",
        title="No secrets",
        content="Never.",
        weight="med",
        **fields,
    )


@pytest.mark.unit
async def test_l177_a_keystone_upsert_looks_its_caller_up_once(monkeypatch):
    storage = _keystone_storage(monkeypatch, trust=2, stored=None)

    doc = await keystones.upsert_keystone(
        body=_rule(scope="tenant"), x_agent_id=None, auth=_author()
    )

    assert doc == {"id": "k-1"}
    storage.get_agent.assert_awaited_once()


@pytest.mark.unit
async def test_l177_the_one_lookup_is_held_to_the_stored_rules_floor(monkeypatch):
    """A trust-1 agent may write its own rule, but not over a stored fleet rule."""
    storage = _keystone_storage(
        monkeypatch, trust=1, stored={"data": {"scope": "fleet"}}
    )

    with pytest.raises(HTTPException) as refused:
        await keystones.upsert_keystone(
            body=_rule(scope="agent", agent_id="author"),
            x_agent_id=None,
            auth=_author(),
        )

    assert refused.value.status_code == 403
    assert "(trust_level=1) < required 2." in str(refused.value.detail)
    storage.get_agent.assert_awaited_once()
    storage.upsert_keystone.assert_not_awaited()
