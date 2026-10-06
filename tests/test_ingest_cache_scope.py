"""B24: the ingest doc-hash cache serves only the caller's own prior ingests.

L-74: commit stamps a caller-supplied ``doc_hash`` it cannot verify, and the
preview lookup was tenant-wide, so any writer in the tenant could commit forged
facts under another document's hash and have every agent's next preview return
them as the cached extraction. L-31: that cache hit also handed back the other
agent's ``run_id``. M-47: the lookup kept only the newest run, so after a
partial run and its re-ingest the cache served the complement as the whole.

The cache is now scoped to the agent and fleet a commit of this preview would
write as, like commit's own pre-dedup, and it serves the union of that caller's
runs.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.models import Memory
from core_api.auth import AuthContext, get_auth_context
from core_api.routes import memories
from core_api.schemas import IngestRequest
from core_api.services import ingest_service
from core_storage_api.services.postgres_service import get_session

CONTENT = "The cache-scope fixture document says alpha and beta."


@pytest.fixture
def offline_preview(monkeypatch):
    """Preview without a provider: a cache miss extracts nothing."""

    async def _config(tenant_id):
        return SimpleNamespace(
            enrichment_provider="fake",
            enrichment_enabled=False,
            default_write_mode="fast",
        )

    monkeypatch.setattr(ingest_service, "resolve_config", _config)
    monkeypatch.setattr(ingest_service, "_chunk_content", AsyncMock(return_value=[]))


# ── preview names the caller's scope ──────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_looks_up_the_callers_own_cache(monkeypatch, offline_preview):
    calls = []

    async def _lookup(tenant_id, doc_hash, **scope):
        calls.append(scope)
        return []

    monkeypatch.setattr(ingest_service, "_find_prior_ingest_by_doc_hash", _lookup)

    await ingest_service.ingest_preview(
        IngestRequest(
            tenant_id="t1", agent_id="agent-a", fleet_id="f1", content=CONTENT
        )
    )

    assert calls == [{"fleet_id": "f1", "agent_id": "agent-a"}]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fleet_id", "agent_row", "expected_fleet"),
    [
        pytest.param(None, {"fleet_id": "home"}, "home", id="omitted-home-fleet"),
        pytest.param(None, None, None, id="omitted-unknown-agent"),
        pytest.param("f1", {"fleet_id": "home"}, "f1", id="named"),
    ],
)
async def test_lookup_uses_the_fleet_commit_would_write_to(
    monkeypatch, fleet_id, agent_row, expected_fleet
):
    """An omitted fleet is the agent's home fleet, as on commit."""
    lookup_agent = AsyncMock(return_value=agent_row)
    storage = SimpleNamespace(find_prior_ingest_by_doc_hash=AsyncMock(return_value=[]))
    monkeypatch.setattr(ingest_service, "lookup_agent", lookup_agent)
    monkeypatch.setattr(ingest_service, "get_storage_client", lambda: storage)

    await ingest_service._find_prior_ingest_by_doc_hash(
        "t1", "h", fleet_id=fleet_id, agent_id="agent-a"
    )

    storage.find_prior_ingest_by_doc_hash.assert_awaited_once_with(
        "t1", "h", fleet_id=expected_fleet, agent_id="agent-a"
    )
    assert lookup_agent.await_count == (0 if fleet_id else 1)


# ── the routes name the agent commit would write as ───────────────────────


async def _preview_as(monkeypatch, auth, agent_id):
    captured = {}

    async def _preview(body):
        captured["agent_id"] = body.agent_id
        return {"facts": []}

    monkeypatch.setattr(memories, "ingest_preview", _preview)
    body = IngestRequest(tenant_id="tenant-1", agent_id=agent_id, content=CONTENT)
    await memories.ingest_preview_endpoint(body, auth)
    return captured["agent_id"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_route_names_the_verified_agent(monkeypatch):
    """The body cannot name a peer to read that peer's cache."""
    auth = AuthContext(tenant_id="tenant-1", agent_id="agent-a")
    assert await _preview_as(monkeypatch, auth, "peer") == "agent-a"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_preview_route_keeps_a_broker_to_its_own_agents(monkeypatch):
    gate = AsyncMock(return_value="broker:install-1")
    monkeypatch.setattr(memories, "broker_owned_agent_id", gate)
    auth = AuthContext(
        tenant_id="tenant-1", is_install_credential=True, install_uuid="install-1"
    )

    assert await _preview_as(monkeypatch, auth, "victim-agent") == "broker:install-1"
    assert gate.await_args.args == ("victim-agent", "install-1", "tenant-1")


@pytest.mark.unit
def test_file_route_names_the_verified_agent(monkeypatch):
    app = FastAPI()
    app.include_router(memories.router, prefix="/api/v1")
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        tenant_id="tenant-1", agent_id="agent-a"
    )
    preview = AsyncMock(return_value={"facts": [], "sections": 0})
    monkeypatch.setattr(memories, "ingest_preview", preview)

    resp = TestClient(app).post(
        "/api/v1/ingest/file",
        files={"file": ("doc.md", CONTENT.encode(), "text/markdown")},
        data={"tenant_id": "tenant-1", "agent_id": "peer"},
    )

    assert resp.status_code == 200, resp.text
    assert preview.await_args.args[0].agent_id == "agent-a"


# ── end to end, against storage ───────────────────────────────────────────


async def _seed(tenant_id, agent_id, run_id, content):
    async with get_session() as session:
        session.add(
            Memory(
                id=uuid4(),
                tenant_id=tenant_id,
                fleet_id="f1",
                agent_id=agent_id,
                memory_type="fact",
                content=content,
                run_id=run_id,
                source_uri="text-input",
                status="active",
                metadata_={
                    "source": "ingest",
                    "doc_hash": ingest_service._doc_hash(tenant_id, CONTENT),
                },
            )
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_peers_forged_run_is_never_served(monkeypatch, offline_preview):
    """Agent A ingested the document in two runs; agent B then committed forged
    facts under the same doc_hash, newest of all."""
    tenant = f"test-ingest-cache-{uuid4().hex[:8]}"
    await _seed(tenant, "agent-a", "run-1", "alpha")
    await _seed(tenant, "agent-a", "run-2", "beta")
    await _seed(tenant, "agent-b", "run-3", "forged")
    monkeypatch.setattr(
        ingest_service, "_prior_ingest_was_complete", AsyncMock(return_value=True)
    )

    def _preview(agent_id):
        return ingest_service.ingest_preview(
            IngestRequest(
                tenant_id=tenant, agent_id=agent_id, fleet_id="f1", content=CONTENT
            )
        )

    own = await _preview("agent-a")
    assert own["cached"] is True
    assert sorted(f["content"] for f in own["facts"]) == ["alpha", "beta"]
    assert own["run_id"] == "run-2"

    third = await _preview("agent-c")
    assert "cached" not in third
    assert "run_id" not in third
