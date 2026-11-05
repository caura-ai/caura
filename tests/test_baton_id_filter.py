"""Handoff batons, HB-10: find the memories a receiver linked to a baton.

A receiver writes its findings with ``metadata.baton_id``. ``baton_id`` on
``caura_list``, ``caura_recall``, REST ``GET /memories``, ``/search`` and
``/recall`` reads them back. It is a storage filter (``_baton_clause``), applied
by the list, the scored search, the entity short-circuit and the successor
lookup alike, and it only narrows: every statement it joins is already bounded
by the visibility predicate, so a caller never sees a row it could not read
without it.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import false
from sqlalchemy.dialects import postgresql

from common.embedding import fake_embedding
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.search import load_and_serialize as las
from core_api.pipeline.steps.search.execute_scored_search import (
    build_scored_search_payload,
)
from core_api.schemas import SearchRequest
from core_storage_api.services import postgres_service as ps

BATON_SQL = "(memories.metadata ->> 'baton_id') = 'b-1'"


def _sql(stmt) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def _record_statements(monkeypatch) -> list:
    statements: list = []

    @contextlib.asynccontextmanager
    async def _session():
        session = AsyncMock()

        async def _execute(stmt, *a, **k):
            statements.append(stmt)
            result = MagicMock()
            result.all.return_value = []
            result.scalars.return_value.all.return_value = []
            return result

        session.execute.side_effect = _execute
        yield session

    monkeypatch.setattr(ps, "get_session", _session)
    monkeypatch.setattr(ps, "get_read_session", _session)
    return statements


# ── storage predicate ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_the_key_is_a_literal_so_the_index_expression_matches():
    # A bound key (``metadata ->> $1``) is not the index's expression under a
    # generic plan; the literal is.
    sql = str(ps._baton_clause("b-1").compile(dialect=postgresql.dialect()))
    assert "->> 'baton_id'" in sql


@pytest.mark.unit
@pytest.mark.parametrize("baton_id", [None, "b-1"])
def test_scored_search_names_the_filter_only_when_asked(baton_id):
    named = ps._scored_search_row_filters(
        tenant_id="t",
        readable_tenant_ids=None,
        fts_guard=false(),
        fleet_ids=None,
        strict_fleet_scoping=False,
        caller_agent_id="a",
        caller_tenant_id=None,
        filter_agent_id=None,
        memory_type_filter=None,
        status_filter=None,
        history_query=False,
        valid_at=None,
        baton_id=baton_id,
    )
    names = [name for name, _ in named]
    assert ("baton_id" in names) is bool(baton_id)
    # Narrowing only: the visibility predicate is still there beside it.
    assert "visibility_scope" in names


@pytest.mark.unit
@pytest.mark.parametrize("baton_id", [None, "b-1"])
async def test_list_applies_the_filter_only_when_asked(monkeypatch, baton_id):
    statements = _record_statements(monkeypatch)
    await ps.PostgresService().memory_list_by_filters(
        tenant_id="t", caller_agent_id="a", baton_id=baton_id
    )
    assert (BATON_SQL in _sql(statements[0])) is bool(baton_id)


@pytest.mark.unit
@pytest.mark.parametrize("baton_id", [None, "b-1"])
async def test_by_id_load_applies_the_filter_only_when_asked(monkeypatch, baton_id):
    statements = _record_statements(monkeypatch)
    await ps.PostgresService().memory_load_by_ids(
        [uuid.uuid4()], "t", baton_id=baton_id
    )
    assert (BATON_SQL in _sql(statements[0])) is bool(baton_id)


@pytest.mark.unit
@pytest.mark.parametrize("baton_id", [None, "b-1"])
async def test_successor_lookup_applies_the_filter_only_when_asked(
    monkeypatch, baton_id
):
    statements = _record_statements(monkeypatch)
    await ps.PostgresService().memory_find_successors(
        [uuid.uuid4()], "t", baton_id=baton_id
    )
    assert statements
    assert all((BATON_SQL in _sql(s)) is bool(baton_id) for s in statements)


# ── core-api plumbing ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_search_request_takes_an_optional_baton_id():
    assert SearchRequest(tenant_id="t", query="q").baton_id is None
    assert SearchRequest(tenant_id="t", query="q", baton_id="b-1").baton_id == "b-1"


@pytest.mark.unit
def test_scored_search_and_explain_payload_carry_it():
    from core_api.services.recall_explain import FILTER_REASONS

    data = {
        "tenant_id": "t",
        "query": "q",
        "baton_id": "b-1",
        "embedding": None,
        "temporal_window": None,
        "boosted_memory_ids": set(),
        "memory_boost_factor": {},
    }
    assert build_scored_search_payload(data, {"top_k": 5}, 5)["baton_id"] == "b-1"
    assert "baton_id" not in build_scored_search_payload(
        {**data, "baton_id": None}, {"top_k": 5}, 5
    )
    assert "baton_id" in FILTER_REASONS


@pytest.mark.unit
async def test_successor_injection_carries_the_filter(monkeypatch):
    from types import SimpleNamespace

    stale = SimpleNamespace(
        Memory=SimpleNamespace(id=uuid.uuid4(), status="outdated"),
        score=0.5,
        entity_links=[],
    )
    sc = MagicMock()
    sc.find_successors = AsyncMock(return_value=[])
    monkeypatch.setattr(las, "get_storage_client", lambda: sc)
    ctx = PipelineContext(
        data={"filtered_rows": [stale], "tenant_id": "t", "baton_id": "b-1"}
    )
    with contextlib.suppress(Exception):
        await las.LoadAndSerialize().execute(ctx)
    assert sc.find_successors.await_args.args[0]["baton_id"] == "b-1"


@pytest.mark.unit
def test_mcp_list_and_recall_expose_baton_id():
    from core_api import mcp_server

    for tool in (mcp_server.caura_list, mcp_server.caura_recall):
        param = inspect.signature(tool).parameters["baton_id"]
        assert param.default is None


@pytest.mark.unit
async def test_mcp_list_sends_it_to_storage(monkeypatch):
    from core_api import mcp_server

    sc = MagicMock()
    sc.list_memories_by_filters = AsyncMock(return_value=[])
    monkeypatch.setattr(mcp_server, "get_storage_client", lambda: sc)
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: None)
    monkeypatch.setattr(mcp_server, "_get_tenant", lambda: "t")
    monkeypatch.setattr(mcp_server, "_refuse_default_agent_on_gateway", lambda _a: None)
    monkeypatch.setattr(
        mcp_server, "_refuse_unowned_agent", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        mcp_server, "_resolve_read_fleet_gate", AsyncMock(return_value=(1, None))
    )
    monkeypatch.setattr(
        mcp_server, "_require_trust", AsyncMock(return_value=(1, None, None))
    )
    await mcp_server.caura_list(agent_id="a", baton_id="b-1")
    assert sc.list_memories_by_filters.await_args.args[0]["baton_id"] == "b-1"


# ── end to end on the database ──────────────────────────────────────────────


async def _write(
    tenant_id: str,
    agent_id: str,
    content: str,
    *,
    baton_id: str | None,
    visibility: str,
) -> str:
    from core_api.clients.storage_client import get_storage_client

    mem = await get_storage_client().create_memory(
        {
            "tenant_id": tenant_id,
            "fleet_id": None,
            "agent_id": agent_id,
            "memory_type": "fact",
            "content": content,
            "weight": 0.5,
            "embedding": fake_embedding(content),
            "content_hash": hashlib.sha256(
                f"{tenant_id}:{content}".encode()
            ).hexdigest(),
            "status": "active",
            "visibility": visibility,
            "metadata_": {"baton_id": baton_id} if baton_id else {},
        }
    )
    return str(mem["id"])


@pytest.fixture
async def baton_rows(tenant_id):
    """Three rows on baton ``B`` from two agents (one private to ``alpha``), two without."""
    baton = f"baton-{uuid.uuid4().hex[:8]}"
    ids = {
        "alpha_private": await _write(
            tenant_id,
            "alpha",
            "release checklist finding one",
            baton_id=baton,
            visibility="scope_agent",
        ),
        "alpha_team": await _write(
            tenant_id,
            "alpha",
            "release checklist finding two",
            baton_id=baton,
            visibility="scope_team",
        ),
        "beta_team": await _write(
            tenant_id,
            "beta",
            "release checklist finding three",
            baton_id=baton,
            visibility="scope_team",
        ),
        "other_1": await _write(
            tenant_id,
            "alpha",
            "release checklist unrelated",
            baton_id=None,
            visibility="scope_team",
        ),
        "other_2": await _write(
            tenant_id,
            "beta",
            "release checklist other baton",
            baton_id="another-baton",
            visibility="scope_team",
        ),
    }
    return baton, ids


@pytest.mark.integration
async def test_list_returns_exactly_what_the_caller_may_read(tenant_id, baton_rows):
    baton, ids = baton_rows
    linked = {ids["alpha_private"], ids["alpha_team"], ids["beta_team"]}
    svc = ps.PostgresService()

    as_alpha = await svc.memory_list_by_filters(
        tenant_id=tenant_id, caller_agent_id="alpha", baton_id=baton
    )
    as_beta = await svc.memory_list_by_filters(
        tenant_id=tenant_id, caller_agent_id="beta", baton_id=baton
    )
    unfiltered_beta = await svc.memory_list_by_filters(
        tenant_id=tenant_id, caller_agent_id="beta"
    )

    assert {str(m.id) for m in as_alpha} == linked
    # beta cannot read alpha's private row, with or without the filter.
    assert {str(m.id) for m in as_beta} == linked - {ids["alpha_private"]}
    assert ids["alpha_private"] not in {str(m.id) for m in unfiltered_beta}


@pytest.mark.integration
@pytest.mark.parametrize("use_pipeline", [True, False], ids=["pipeline", "legacy"])
async def test_recall_returns_exactly_what_the_caller_may_read(
    tenant_id, baton_rows, use_pipeline, monkeypatch
):
    from core_api.services import memory_service

    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", use_pipeline)
    baton, ids = baton_rows
    linked = {ids["alpha_private"], ids["alpha_team"], ids["beta_team"]}

    async def recall(agent: str) -> set[str]:
        hits = await memory_service.search_memories(
            tenant_id,
            "release checklist finding",
            caller_agent_id=agent,
            caller_tenant_id=tenant_id,
            top_k=20,
            min_similarity=0.0,
            baton_id=baton,
        )
        return {str(h.id) for h in hits}

    assert await recall("alpha") == linked
    assert await recall("beta") == linked - {ids["alpha_private"]}
