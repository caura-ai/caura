"""core-api tells storage which tenant the caller's own ``scope_agent`` rows
live in, and refuses a by-id read of a sibling tenant's private row.

``agent_id`` is unique per tenant only, so an agent-bound cross-tenant
credential for ``rollup-bot`` in ``home`` is not the author of ``sibling``'s
``rollup-bot`` notes. The storage predicate pairs the identity with
``caller_tenant_id`` (see core-storage-api/tests/test_scope_agent_home_tenant.py);
these tests pin the core-api half: every read that carries ``caller_agent_id``
also carries the caller's HOME tenant, which differs from the request tenant on
a pinned sibling read.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_by_id_gate_refuses_a_sibling_tenants_private_row():
    from core_api.services.agent_service import authorize_memory_access

    kwargs = {
        "visibility": "scope_agent",
        "owner_agent_id": "rollup-bot",
        "fleet_id": None,
    }
    assert not await authorize_memory_access(
        "sibling", "rollup-bot", caller_tenant_id="home", **kwargs
    )
    assert await authorize_memory_access(
        "home", "rollup-bot", caller_tenant_id="home", **kwargs
    )
    # A tenant credential (no agent identity) keeps tenant-wide access.
    assert await authorize_memory_access(
        "sibling", None, caller_tenant_id="home", **kwargs
    )


async def test_scored_search_forwards_the_callers_home_tenant(monkeypatch):
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search import execute_scored_search as mod

    sc = MagicMock()
    sc.scored_search = AsyncMock(return_value=[])
    monkeypatch.setattr(mod, "get_storage_client", lambda: sc)
    ctx = PipelineContext(
        data={
            "tenant_id": "sibling",
            "query": "q",
            "embedding": [0.0] * 8,
            "search_params": {"top_k": 5},
            "temporal_window": None,
            "boosted_memory_ids": [],
            "memory_boost_factor": {},
            "caller_agent_id": "rollup-bot",
            "caller_tenant_id": "home",
        }
    )
    await mod.ExecuteScoredSearch().execute(ctx)
    sent = sc.scored_search.await_args.args[0]
    assert sent["caller_agent_id"] == "rollup-bot"
    assert sent["caller_tenant_id"] == "home"


async def test_search_memories_threads_the_home_tenant_into_the_pipeline(monkeypatch):
    from core_api.services import memory_service

    seen: dict = {}

    async def fake_pipeline(tenant_id, query, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(memory_service, "_search_memories_pipeline", fake_pipeline)
    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", True)
    await memory_service.search_memories(
        "sibling", "q", caller_agent_id="rollup-bot", caller_tenant_id="home"
    )
    assert seen["caller_tenant_id"] == "home"


async def test_rest_list_pinned_to_a_sibling_sends_the_home_tenant(monkeypatch):
    from core_api.auth import AuthContext
    from core_api.routes import memories as memories_routes

    monkeypatch.setattr(memories_routes, "log_cross_tenant_read", AsyncMock())
    listed = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "core_api.clients.storage_client.CoreStorageClient.list_memories_by_filters",
        listed,
    )
    monkeypatch.setattr(memories_routes, "_gate_fleet_read", AsyncMock())
    auth = AuthContext(
        tenant_id="home", agent_id="rollup-bot", readable_tenant_ids=["home", "sibling"]
    )

    # Every parameter explicit: calling the endpoint function directly skips
    # FastAPI's dependency resolution (see test_cross_tenant_audit_surfaces).
    await memories_routes.list_memories(
        tenant_id="sibling",
        fleet_id=None,
        agent_id=None,
        scope=None,
        written_by=None,
        weight_min=None,
        weight_max=None,
        memory_type=None,
        exclude_memory_types=None,
        created_after=None,
        created_before=None,
        status=None,
        visibility=None,
        run_id=None,
        cursor=None,
        sort="created_at",
        order="desc",
        offset=0,
        limit=25,
        include_deleted=False,
        auth=auth,
    )
    payload = listed.await_args.args[-1]
    assert payload["tenant_id"] == "sibling"
    assert payload["caller_agent_id"] == "rollup-bot"
    assert payload["caller_tenant_id"] == "home"
