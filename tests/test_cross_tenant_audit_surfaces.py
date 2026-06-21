"""Per-surface assertions that ``log_cross_tenant_read`` fires from
every cross-tenant read site (audit T2).

Existing coverage (already shipped):
  - REST entity routes (PR-A2): ``rest_entities_list``, ``rest_graph``,
    ``rest_entity_get`` — see ``tests/test_entity_routes_cross_tenant.py``.

This module covers the remaining surfaces:
  - ``memclaw_recall``      (MCP)
  - ``memclaw_doc`` search   (MCP)
  - ``memclaw_list``         (MCP)
  - ``memclaw_stats``        (MCP)
  - ``rest_memories_list``   (REST)
  - ``rest_documents_search``(REST)

Each test sets up a cross-tenant credential, runs the handler against
mocks that return rows from at least one sibling tenant, spies on
``log_cross_tenant_read``, and asserts the spy was called with the
correct ``surface`` tag and ``source_tenants`` set. A regression that
silently drops the audit emission on any of these paths would fail the
corresponding test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spy_log(monkeypatch, module_or_path) -> AsyncMock:
    """Patch ``log_cross_tenant_read`` on the given module or dotted
    path with an AsyncMock; return the spy.

    The audit helper is imported into many modules; patching at the
    call site (rather than at ``core_api.services.audit_service``) is
    what guarantees the spy actually intercepts the handler's call.
    """
    spy = AsyncMock(name="log_cross_tenant_read")
    if isinstance(module_or_path, str):
        monkeypatch.setattr(module_or_path, spy)
    else:
        monkeypatch.setattr(module_or_path, "log_cross_tenant_read", spy)
    return spy


class _MemoryRow:
    """Minimal stand-in for a memory row — exposes the ``tenant_id``
    attribute the audit code reads to compute ``result_count_by_tenant``,
    plus enough other attributes that handlers' serializers don't
    explode while we focus on the audit emission contract."""

    def __init__(self, tenant_id: str):
        self.id = UUID("00000000-0000-0000-0000-000000000001")
        self.tenant_id = tenant_id
        self.fleet_id = None
        self.agent_id = "test-agent"
        self.memory_type = "fact"
        self.title = None
        self.content = "x"
        self.weight = 0.5
        self.source_uri = None
        self.run_id = None
        self.metadata_ = {}
        # MemoryOut validates ``created_at`` as a real datetime; the
        # handler serializes via _memory_to_out which reads this attr.
        self.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        self.expires_at = None
        self.subject_entity_id = None
        self.predicate = None
        self.object_value = None
        self.ts_valid_start = None
        self.ts_valid_end = None
        self.status = "active"
        self.visibility = "scope_team"
        self.recall_count = 0
        self.last_recalled_at = None
        self.supersedes_id = None
        self.deleted_at = None
        self.embedding = None

    def model_dump(self, mode: str = "python"):  # noqa: ARG002
        return {"id": self.id, "tenant_id": self.tenant_id}


# ===========================================================================
# MCP surfaces (cross-tenant ContextVars + spy on mcp_server.log_cross_tenant_read)
# ===========================================================================


@pytest.fixture
def cross_tenant_mcp_env(mcp_env, monkeypatch):
    """Extends the standard ``mcp_env`` fixture with cross-tenant ContextVars
    (home tenant + one sibling readable). Returns the spy installed on
    ``mcp_server.log_cross_tenant_read``."""
    from core_api import mcp_server

    # Pin the context vars to model a cross-tenant credential.
    monkeypatch.setattr(mcp_server, "_get_tenant", lambda: "tenant-home")
    monkeypatch.setattr(
        mcp_server, "_get_readable_tenants", lambda: ["tenant-home", "tenant-sibling"]
    )
    spy = _spy_log(monkeypatch, mcp_server)
    return spy


# --- memclaw_recall ---------------------------------------------------------


async def test_memclaw_recall_emits_cross_tenant_audit(
    cross_tenant_mcp_env, mcp_env, monkeypatch
):
    from core_api import mcp_server

    # Search returns one row from each tenant — the sibling row drives
    # the audit emission.
    mcp_env["service"]("search_memories").return_value = [
        _MemoryRow("tenant-home"),
        _MemoryRow("tenant-sibling"),
    ]
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=SimpleNamespace(recall_boost=False, graph_expand=False)),
    )
    monkeypatch.setattr(
        "core_api.repositories.agent_repo.get_by_id", AsyncMock(return_value=None)
    )

    await mcp_server.memclaw_recall(query="cross-tenant probe", agent_id="a1")

    cross_tenant_mcp_env.assert_awaited_once()
    kwargs = cross_tenant_mcp_env.await_args.kwargs
    assert kwargs["surface"] == "memclaw_recall"
    assert kwargs["source_tenants"] == ["tenant-sibling"]
    assert kwargs["home_tenant_id"] == "tenant-home"


# --- memclaw_list -----------------------------------------------------------


async def test_memclaw_list_emits_cross_tenant_audit(
    cross_tenant_mcp_env, mcp_env, monkeypatch
):
    """``memclaw_list`` widens via the same context-var path and
    audits with ``surface=memclaw_list``."""
    from core_api import mcp_server

    monkeypatch.setattr(
        "core_api.repositories.memory_repo.list_by_filters",
        AsyncMock(
            return_value=[_MemoryRow("tenant-home"), _MemoryRow("tenant-sibling")]
        ),
    )
    # Trust gate stubbed so the handler doesn't 403 before reaching list_by_filters.
    monkeypatch.setattr(
        mcp_server, "_require_trust", AsyncMock(return_value=(3, False, None))
    )

    await mcp_server.memclaw_list(scope="fleet", agent_id="a1")

    cross_tenant_mcp_env.assert_awaited()
    kwargs = cross_tenant_mcp_env.await_args.kwargs
    assert kwargs["surface"] == "memclaw_list"
    assert "tenant-sibling" in kwargs["source_tenants"]
    counts = kwargs.get("result_count_by_tenant") or {}
    assert counts.get("tenant-sibling", 0) >= 1


# --- memclaw_stats ---------------------------------------------------------


async def test_memclaw_stats_emits_cross_tenant_audit(
    cross_tenant_mcp_env, mcp_env, monkeypatch
):
    """``memclaw_stats`` with ``scope=fleet`` widens to readable_tenant_ids."""
    from core_api import mcp_server

    monkeypatch.setattr(
        mcp_server, "_require_trust", AsyncMock(return_value=(3, False, None))
    )
    # ``compute_memory_stats`` is imported INSIDE the handler — patch
    # at the source so the late import resolves to our mock.
    monkeypatch.setattr(
        "core_api.services.memory_stats.compute_memory_stats",
        AsyncMock(
            return_value={
                "total": 5,
                "by_type": {"fact": 5},
                "by_agent": {},
                "by_status": {"active": 5},
                "by_tenant": {"tenant-home": 3, "tenant-sibling": 2},
            }
        ),
    )

    await mcp_server.memclaw_stats(scope="fleet", agent_id="a1")

    cross_tenant_mcp_env.assert_awaited()
    kwargs = cross_tenant_mcp_env.await_args.kwargs
    assert kwargs["surface"] == "memclaw_stats"
    assert "tenant-sibling" in kwargs["source_tenants"]


# --- memclaw_doc (search) ---------------------------------------------------


async def test_memclaw_doc_search_emits_cross_tenant_audit(
    cross_tenant_mcp_env, mcp_env, monkeypatch
):
    """``op=search`` on ``memclaw_doc`` widens via ``readable_tenant_ids``
    and audits with ``surface=memclaw_doc_search``. The handler embeds
    the query, calls ``document_repo.search``, then emits."""
    from core_api import mcp_server

    fake_doc = SimpleNamespace(tenant_id="tenant-sibling", id="d1", collection="things")
    monkeypatch.setattr(
        "core_api.repositories.document_repo.search",
        AsyncMock(return_value=[(fake_doc, 0.91)]),
    )
    # The embed step is gated by an embed-provider call we'd rather not
    # touch in a unit test — patch the helper that wraps it.
    monkeypatch.setattr(
        "core_api.services.memory_service.get_query_embedding",
        AsyncMock(return_value=[0.1] * 8),
    )
    # ``tenant_config`` resolution short-circuits to a fake.
    monkeypatch.setattr(
        "core_api.services.organization_settings.resolve_config",
        AsyncMock(return_value=SimpleNamespace(embedding_model=None)),
    )

    await mcp_server.memclaw_doc(op="search", query="hello world", agent_id="a1")

    cross_tenant_mcp_env.assert_awaited()
    kwargs = cross_tenant_mcp_env.await_args.kwargs
    assert kwargs["surface"] == "memclaw_doc_search"
    assert "tenant-sibling" in kwargs["source_tenants"]


# ===========================================================================
# REST surfaces (build AuthContext explicitly + spy on the route's import)
# ===========================================================================


def _cross_tenant_auth():
    """Return an AuthContext modeling a cross-tenant credential —
    home=``home``, readable=[home, sibling]."""
    from core_api.auth import AuthContext

    return AuthContext(
        tenant_id="home",
        agent_id="a1",
        readable_tenant_ids=["home", "sibling"],
    )


# --- rest_memories_list ----------------------------------------------------


async def test_rest_memories_list_emits_cross_tenant_audit(monkeypatch):
    """``GET /api/v1/memories`` widens when ``tenant_id`` is omitted on
    a cross-tenant credential; the broad fan-out path emits one event
    per source tenant with ``surface=rest_memories_list``."""
    from core_api.routes import memories as memories_routes

    spy = _spy_log(monkeypatch, memories_routes)
    # The route does a late import of ``memory_repo`` so we patch it
    # at the source.
    monkeypatch.setattr(
        "core_api.repositories.memory_repo.list_by_filters",
        AsyncMock(return_value=[_MemoryRow("home"), _MemoryRow("sibling")]),
    )

    auth = _cross_tenant_auth()
    db = MagicMock()

    await memories_routes.list_memories(
        tenant_id=None,  # broad — triggers widening
        fleet_id=None,
        agent_id=None,
        memory_type=None,
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
        db=db,
    )

    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["surface"] == "rest_memories_list"
    assert "sibling" in kwargs["source_tenants"]


# --- rest_documents_search -------------------------------------------------


async def test_rest_documents_search_emits_cross_tenant_audit(monkeypatch):
    """The REST documents search endpoint mirrors ``memclaw_doc`` op=search
    on the cross-tenant audit emission, but with ``surface=rest_documents_search``."""
    from core_api.routes import documents as documents_routes

    spy = _spy_log(monkeypatch, documents_routes)
    # The REST search route now routes through the storage client
    # (``sc.search_documents_vector``), which returns plain dict rows
    # carrying ``tenant_id`` for the cross-tenant audit count.
    fake_rows = [
        {
            "tenant_id": "sibling",
            "collection": "things",
            "doc_id": "docX",
            "data": {},
            "similarity": 0.91,
        }
    ]
    fake_sc = SimpleNamespace(search_documents_vector=AsyncMock(return_value=fake_rows))
    monkeypatch.setattr(documents_routes, "get_storage_client", lambda: fake_sc)
    monkeypatch.setattr(documents_routes, "check_and_increment", AsyncMock())
    # The route imports ``get_embedding`` at module top-level, so patch it
    # in the route module's namespace (not at the source).
    monkeypatch.setattr(documents_routes, "get_embedding", AsyncMock(return_value=[0.1] * 8))

    body = documents_routes.DocSearchRequest(
        tenant_id="home",
        query="hello",
        top_k=5,
    )
    auth = _cross_tenant_auth()
    db = MagicMock()

    await documents_routes.search_documents(body=body, auth=auth, db=db)

    spy.assert_awaited()
    kwargs = spy.await_args.kwargs
    assert kwargs["surface"] == "rest_documents_search"
    assert "sibling" in kwargs["source_tenants"]
