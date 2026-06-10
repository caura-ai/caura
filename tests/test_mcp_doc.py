"""Unit tests for ``memclaw_doc`` (op: write | read | query | delete).

Covers:
- Unknown op → ``INVALID_ARGUMENTS`` envelope.
- Per-op required-parameter validation (doc_id, data).
- Happy paths for all four ops (action/payload/count fields).
- ``op=read`` not-found → "Not found:" text.
- ``op=delete`` not-found → structured error envelope.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from core_api import mcp_server
from core_api.constants import VECTOR_DIM
from tests._mcp_test_helpers import parse_envelope, strip_latency

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _DocRow:
    def __init__(self, doc_id: str = "acme", collection: str = "customers"):
        self.collection = collection
        self.doc_id = doc_id
        self.data = {"plan": "business"}
        self.updated_at = datetime.now(timezone.utc)


class _UpsertRow:
    """Stand-in for the unlabeled Row returned by upsert_returning_xmax.
    xmax sits at index 3 (id=0, created_at=1, updated_at=2, xmax=3).
    """

    def __init__(self, xmax: int):
        self._data = (None, None, None, xmax)

    def __getitem__(self, idx):
        return self._data[idx]


async def test_doc_invalid_op_errors(mcp_env):
    out = await mcp_server.memclaw_doc(op="oops", collection="c")
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"
    assert payload["error"]["details"]["expected_ops"] == [
        "delete",
        "list_collections",
        "query",
        "read",
        "search",
        "write",
    ]


async def test_doc_write_missing_doc_id(mcp_env):
    out = await mcp_server.memclaw_doc(op="write", collection="c", data={"k": 1})
    assert "op=write requires 'doc_id'" in strip_latency(out)


async def test_doc_write_missing_data(mcp_env):
    out = await mcp_server.memclaw_doc(op="write", collection="c", doc_id="x")
    assert "op=write requires 'data'" in strip_latency(out)


async def test_doc_write_happy_path_new(mcp_env, monkeypatch):
    """xmax=0 means a brand-new row was inserted."""
    monkeypatch.setattr(
        "core_api.repositories.document_repo.upsert_returning_xmax",
        _async_return(_UpsertRow(xmax=0)),
    )
    out = await mcp_server.memclaw_doc(
        op="write", collection="customers", doc_id="acme", data={"plan": "enterprise"}
    )
    payload = parse_envelope(out)
    assert payload["ok"] is True
    assert payload["action"] == "created"
    assert payload["collection"] == "customers"
    assert payload["doc_id"] == "acme"
    assert payload["indexed"] is False  # no data["summary"] → not indexed


async def test_doc_write_happy_path_updated(mcp_env, monkeypatch):
    """xmax!=0 means an existing row was updated."""
    monkeypatch.setattr(
        "core_api.repositories.document_repo.upsert_returning_xmax",
        _async_return(_UpsertRow(xmax=42)),
    )
    out = await mcp_server.memclaw_doc(
        op="write", collection="customers", doc_id="acme", data={"plan": "pro"}
    )
    payload = parse_envelope(out)
    assert payload["action"] == "updated"


async def test_doc_read_missing_doc_id(mcp_env):
    out = await mcp_server.memclaw_doc(op="read", collection="customers")
    assert "op=read requires 'doc_id'" in strip_latency(out)


async def test_doc_read_not_found(mcp_env, monkeypatch):
    monkeypatch.setattr(
        "core_api.repositories.document_repo.get_by_doc_id", _async_return(None)
    )
    out = await mcp_server.memclaw_doc(
        op="read", collection="customers", doc_id="ghost"
    )
    assert "Not found: customers/ghost" in strip_latency(out)


async def test_doc_read_happy_path(mcp_env, monkeypatch):
    monkeypatch.setattr(
        "core_api.repositories.document_repo.get_by_doc_id",
        _async_return(_DocRow("acme")),
    )
    out = await mcp_server.memclaw_doc(op="read", collection="customers", doc_id="acme")
    payload = parse_envelope(out)
    assert payload["doc_id"] == "acme"
    assert payload["data"] == {"plan": "business"}


async def test_doc_query_happy_path(mcp_env, monkeypatch):
    rows = [_DocRow("acme"), _DocRow("initech")]
    monkeypatch.setattr(
        "core_api.repositories.document_repo.query", _async_return(rows)
    )
    out = await mcp_server.memclaw_doc(
        op="query", collection="customers", where={"plan": "business"}
    )
    payload = parse_envelope(out)
    assert payload["count"] == 2
    assert payload["collection"] == "customers"
    assert [r["doc_id"] for r in payload["results"]] == ["acme", "initech"]


async def test_doc_query_where_defaults_to_empty_dict(mcp_env, monkeypatch):
    captured = {}

    async def fake_query(db, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr("core_api.repositories.document_repo.query", fake_query)
    await mcp_server.memclaw_doc(op="query", collection="customers")
    assert captured["where"] == {}


async def test_doc_delete_missing_doc_id(mcp_env):
    out = await mcp_server.memclaw_doc(op="delete", collection="customers")
    assert "op=delete requires 'doc_id'" in strip_latency(out)


async def test_doc_delete_not_found_envelope(mcp_env):
    """The DELETE scalar_one_or_none path returns a {"error": "…"} JSON blob."""
    # db.execute(...) → result with scalar_one_or_none() returning None.
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    mcp_env["db"].execute.return_value = result_mock

    out = await mcp_server.memclaw_doc(
        op="delete", collection="customers", doc_id="ghost"
    )
    payload = parse_envelope(out)
    assert "not found" in payload["error"].lower()
    assert "ghost" in payload["error"]


async def test_doc_delete_happy_path(mcp_env):
    from uuid import uuid4

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = uuid4()
    mcp_env["db"].execute.return_value = result_mock

    out = await mcp_server.memclaw_doc(
        op="delete", collection="customers", doc_id="acme"
    )
    payload = parse_envelope(out)
    assert payload["ok"] is True
    assert payload["deleted"] is True
    assert payload["doc_id"] == "acme"
    mcp_env["db"].commit.assert_awaited_once()


async def test_doc_auth_failure_shortcircuits(monkeypatch):
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: mcp_server._AUTH_ERROR)
    out = await mcp_server.memclaw_doc(op="read", collection="c", doc_id="d")
    assert out == mcp_server._AUTH_ERROR


# ---------------------------------------------------------------------------
# op=list_collections
# ---------------------------------------------------------------------------


async def test_doc_list_collections_happy_path(mcp_env, monkeypatch):
    """Returns each collection with its document count."""
    rows = [("customers", 3), ("onboarding_guides", 1), ("proposals", 2)]
    monkeypatch.setattr(
        "core_api.repositories.document_repo.list_collections", _async_return(rows)
    )
    out = await mcp_server.memclaw_doc(op="list_collections")
    payload = parse_envelope(out)
    assert payload["count"] == 3
    assert payload["collections"] == [
        {"name": "customers", "count": 3},
        {"name": "onboarding_guides", "count": 1},
        {"name": "proposals", "count": 2},
    ]


async def test_doc_list_collections_empty_tenant(mcp_env, monkeypatch):
    """Empty tenant returns an empty list, not an error."""
    monkeypatch.setattr(
        "core_api.repositories.document_repo.list_collections", _async_return([])
    )
    out = await mcp_server.memclaw_doc(op="list_collections")
    payload = parse_envelope(out)
    assert payload["collections"] == []
    assert payload["count"] == 0


async def test_doc_list_collections_does_not_require_collection(mcp_env, monkeypatch):
    """Unlike every other op, list_collections has no required params — the
    whole point is to discover collection names when you don't know them yet.
    """
    monkeypatch.setattr(
        "core_api.repositories.document_repo.list_collections", _async_return([])
    )
    out = await mcp_server.memclaw_doc(op="list_collections")
    payload = parse_envelope(out)
    assert "error" not in payload


async def test_doc_list_collections_passes_fleet_id_filter(mcp_env, monkeypatch):
    """fleet_id scopes the count; not a mandatory param."""
    captured = {}

    async def fake_list(db, *, tenant_id, fleet_id=None, readable_tenant_ids=None):  # noqa: ARG001
        captured["fleet_id"] = fleet_id
        return [("customers", 1)]

    monkeypatch.setattr(
        "core_api.repositories.document_repo.list_collections", fake_list
    )
    await mcp_server.memclaw_doc(op="list_collections", fleet_id="caura-rnd-fleet")
    assert captured["fleet_id"] == "caura-rnd-fleet"


async def test_doc_write_requires_collection(mcp_env):
    """With `collection` now optional in the signature (to accommodate
    list_collections), the other ops must still enforce it explicitly."""
    out = await mcp_server.memclaw_doc(op="write", doc_id="x", data={"k": 1})
    assert "op=write requires 'collection'" in strip_latency(out)


async def test_doc_read_requires_collection(mcp_env):
    out = await mcp_server.memclaw_doc(op="read", doc_id="x")
    assert "op=read requires 'collection'" in strip_latency(out)


async def test_doc_query_requires_collection(mcp_env):
    out = await mcp_server.memclaw_doc(op="query")
    assert "op=query requires 'collection'" in strip_latency(out)


# ---------------------------------------------------------------------------
# op=write semantic indexing: only data["summary"] is ever embedded
# ---------------------------------------------------------------------------


async def test_doc_write_summary_embeds_and_forwards(mcp_env, monkeypatch):
    """When data["summary"] is present, the server embeds that string and
    forwards the vector to the repo. Response reports indexed=True."""
    captured: dict = {}

    async def fake_upsert(db, **kwargs):  # noqa: ARG001
        captured.update(kwargs)
        return _UpsertRow(xmax=0)

    async def fake_embed(text):
        captured["embed_text"] = text
        return [0.1] * VECTOR_DIM

    monkeypatch.setattr(
        "core_api.repositories.document_repo.upsert_returning_xmax", fake_upsert
    )
    monkeypatch.setattr("common.embedding.get_embedding", fake_embed)

    out = await mcp_server.memclaw_doc(
        op="write",
        collection="onboarding_guides",
        doc_id="claude-code-setup",
        data={
            "summary": "Claude Code setup runbook",
            "content": "Some 5KB markdown body that must NOT be embedded",
        },
    )
    payload = parse_envelope(out)
    assert payload["ok"] is True
    assert payload["indexed"] is True
    # Only the summary is embedded — the body is stored but not indexed.
    assert captured["embed_text"] == "Claude Code setup runbook"
    assert len(captured["embedding"]) == VECTOR_DIM


async def test_doc_write_no_summary_stores_unindexed(mcp_env, monkeypatch):
    """Non-skills writes without data["summary"] persist without an
    embedding — the "I don't need semantic search" path stays open."""
    called = {"hit": False}

    async def should_not_embed(text):  # noqa: ARG001
        called["hit"] = True
        return [0.0] * VECTOR_DIM

    monkeypatch.setattr(
        "core_api.repositories.document_repo.upsert_returning_xmax",
        _async_return(_UpsertRow(xmax=0)),
    )
    monkeypatch.setattr("common.embedding.get_embedding", should_not_embed)

    out = await mcp_server.memclaw_doc(
        op="write",
        collection="customers",
        doc_id="acme",
        data={"plan": "enterprise"},
    )
    payload = parse_envelope(out)
    assert payload["ok"] is True
    assert payload["indexed"] is False
    assert called["hit"] is False


async def test_doc_write_summary_empty_string_is_rejected(mcp_env, monkeypatch):
    """When summary is provided but blank, embedding would be noise — reject."""
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.0] * VECTOR_DIM)
    )
    out = await mcp_server.memclaw_doc(
        op="write",
        collection="c",
        doc_id="d",
        data={"summary": "   "},
    )
    assert "non-empty string" in strip_latency(out)


async def test_doc_write_embedding_provider_failure_aborts(mcp_env, monkeypatch):
    """If the embedding provider returns None, the write is aborted —
    better than silently persisting the doc without an index."""
    monkeypatch.setattr("common.embedding.get_embedding", _async_return(None))
    out = await mcp_server.memclaw_doc(
        op="write",
        collection="c",
        doc_id="d",
        data={"summary": "valid summary string"},
    )
    assert "embedding provider returned no vector" in strip_latency(out).lower()


# ---------------------------------------------------------------------------
# op=search
# ---------------------------------------------------------------------------


async def test_doc_search_without_collection_spans_all(mcp_env, monkeypatch):
    """Collection is intentionally optional on search — omitting it triggers
    the cross-collection strategy. Handler must pass collection=None to the
    repo, not reject the call."""
    captured: dict = {}

    async def fake_search(db, **kwargs):  # noqa: ARG001
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    monkeypatch.setattr("core_api.repositories.document_repo.search", fake_search)

    out = await mcp_server.memclaw_doc(op="search", query="onboarding")
    payload = parse_envelope(out)
    # No 422 — broad search is a legitimate call
    assert "error" not in payload
    assert captured["collection"] is None


async def test_doc_search_broad_results_include_per_row_collection(
    mcp_env, monkeypatch
):
    """Each result row must include its own `collection` so the caller can
    follow up with op=read across mixed collections."""
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    pairs = [
        (_DocRow("acme", collection="customers"), 0.9),
        (_DocRow("guide-1", collection="onboarding_guides"), 0.6),
    ]
    monkeypatch.setattr(
        "core_api.repositories.document_repo.search", _async_return(pairs)
    )
    out = await mcp_server.memclaw_doc(op="search", query="signup flow")
    payload = parse_envelope(out)
    assert payload["collection"] is None
    assert payload["results"][0]["collection"] == "customers"
    assert payload["results"][1]["collection"] == "onboarding_guides"


async def test_doc_search_requires_query(mcp_env):
    out = await mcp_server.memclaw_doc(op="search", collection="c")
    assert "op=search requires a non-empty 'query'" in strip_latency(out)


async def test_doc_search_empty_query_rejected(mcp_env):
    """Whitespace-only query is as useless as no query."""
    out = await mcp_server.memclaw_doc(op="search", collection="c", query="   ")
    assert "op=search requires a non-empty 'query'" in strip_latency(out)


async def test_doc_search_happy_path(mcp_env, monkeypatch):
    """Happy path: embedding → repo.search → results sorted by similarity."""
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    pairs = [(_DocRow("acme"), 0.92), (_DocRow("initech"), 0.81)]
    monkeypatch.setattr(
        "core_api.repositories.document_repo.search", _async_return(pairs)
    )
    out = await mcp_server.memclaw_doc(
        op="search",
        collection="customers",
        query="payment plans",
    )
    payload = parse_envelope(out)
    assert payload["count"] == 2
    assert payload["collection"] == "customers"
    assert payload["results"][0]["doc_id"] == "acme"
    assert payload["results"][0]["similarity"] == 0.92
    assert payload["results"][1]["doc_id"] == "initech"


async def test_doc_search_empty_results(mcp_env, monkeypatch):
    """No indexed docs / no matches → empty list, not error."""
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    monkeypatch.setattr("core_api.repositories.document_repo.search", _async_return([]))
    out = await mcp_server.memclaw_doc(op="search", collection="c", query="anything")
    payload = parse_envelope(out)
    assert payload["count"] == 0
    assert payload["results"] == []


async def test_doc_search_top_k_capped_at_50(mcp_env, monkeypatch):
    """top_k above 50 is capped server-side."""
    captured: dict = {}

    async def fake_search(db, **kwargs):  # noqa: ARG001
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    monkeypatch.setattr("core_api.repositories.document_repo.search", fake_search)

    await mcp_server.memclaw_doc(op="search", collection="c", query="q", top_k=9999)
    assert captured["top_k"] == 50


async def test_doc_search_embedding_provider_failure_aborts(mcp_env, monkeypatch):
    """Provider failure → no search attempt, caller sees a clear error."""
    monkeypatch.setattr("common.embedding.get_embedding", _async_return(None))
    out = await mcp_server.memclaw_doc(op="search", collection="c", query="anything")
    assert "embedding provider returned no vector" in strip_latency(out).lower()


# ---------------------------------------------------------------------------
# Read-op widening: ``_readable_tenant_ids_var`` reaches the repo call
# (audit T1)
# ---------------------------------------------------------------------------
#
# Cross-tenant credentials surface as a non-empty
# ``_readable_tenant_ids_var``; the tool reads it via
# ``_get_readable_tenants()`` and passes the list as ``readable_tenant_ids``
# to whichever ``document_repo`` function backs the requested op:
#
#   list_collections → document_repo.list_collections
#   read             → document_repo.get_by_doc_id
#   query            → document_repo.query
#   search           → document_repo.search
#
# T1 locks in the wiring: a regression that silently drops the list on
# any of those four paths would fail the corresponding test below.


async def _capture_readable_tenant_ids(monkeypatch, repo_attr: str, return_value):
    """Patch the named ``document_repo`` function with a capture stub
    that records the ``readable_tenant_ids`` kwarg and returns the
    given value."""
    captured: dict = {}

    async def fake(*args, **kwargs):  # noqa: ARG001
        captured["readable_tenant_ids"] = kwargs.get("readable_tenant_ids")
        return return_value

    monkeypatch.setattr(f"core_api.repositories.document_repo.{repo_attr}", fake)
    return captured


async def test_doc_list_collections_passes_readable_tenants(mcp_env, monkeypatch):
    """``op=list_collections`` widens to the readable set when the
    caller's credential is cross-tenant."""
    monkeypatch.setattr(
        mcp_server, "_get_readable_tenants", lambda: ["home", "sibling"]
    )
    captured = await _capture_readable_tenant_ids(monkeypatch, "list_collections", [])
    await mcp_server.memclaw_doc(op="list_collections")
    assert captured["readable_tenant_ids"] == ["home", "sibling"]


async def test_doc_list_collections_single_tenant_passes_none(mcp_env, monkeypatch):
    """Single-tenant credential: ``_get_readable_tenants()`` returns
    ``[]`` → the tool collapses it to ``None`` before calling the repo
    (so the repo's single-tenant fast path runs)."""
    monkeypatch.setattr(mcp_server, "_get_readable_tenants", lambda: [])
    captured = await _capture_readable_tenant_ids(monkeypatch, "list_collections", [])
    await mcp_server.memclaw_doc(op="list_collections")
    assert captured["readable_tenant_ids"] is None


async def test_doc_read_passes_readable_tenants(mcp_env, monkeypatch):
    """``op=read`` widens via ``readable_tenant_ids``. The repo can
    then resolve a doc that lives in a sibling tenant when the caller
    is authorized to read from it."""
    monkeypatch.setattr(
        mcp_server, "_get_readable_tenants", lambda: ["home", "sibling"]
    )
    captured = await _capture_readable_tenant_ids(
        monkeypatch, "get_by_doc_id", _DocRow()
    )
    await mcp_server.memclaw_doc(op="read", collection="customers", doc_id="acme")
    assert captured["readable_tenant_ids"] == ["home", "sibling"]


async def test_doc_query_passes_readable_tenants(mcp_env, monkeypatch):
    """``op=query`` (filter-by-data) widens the same way."""
    monkeypatch.setattr(
        mcp_server, "_get_readable_tenants", lambda: ["home", "sibling"]
    )
    captured = await _capture_readable_tenant_ids(monkeypatch, "query", [])
    await mcp_server.memclaw_doc(op="query", collection="customers", where={"k": 1})
    assert captured["readable_tenant_ids"] == ["home", "sibling"]


async def test_doc_search_passes_readable_tenants(mcp_env, monkeypatch):
    """``op=search`` (vector recall) widens the same way. The audit
    emission has its own assertion in
    ``tests/test_cross_tenant_audit_surfaces.py``; this test only
    confirms the wiring from the context var to the repo arg."""
    monkeypatch.setattr(
        mcp_server, "_get_readable_tenants", lambda: ["home", "sibling"]
    )
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    captured = await _capture_readable_tenant_ids(monkeypatch, "search", [])
    await mcp_server.memclaw_doc(op="search", query="hello")
    assert captured["readable_tenant_ids"] == ["home", "sibling"]


# ---------------------------------------------------------------------------
# Active-only skill discovery (MCP-direct delivery)
#
# The agent-facing memclaw_doc surface must expose only ``status='active'``
# skills to opted-in tenants — candidate / staged / quarantined skills are
# in-flight or blocked and must not surface. Gated on
# ``skills_factory.enabled``: a non-opted-in tenant's reads are unchanged.
# ---------------------------------------------------------------------------


class _SkillRow:
    def __init__(self, doc_id: str, status: str, tenant_id: str = "test-tenant"):
        self.collection = "skills"
        self.doc_id = doc_id
        self.tenant_id = tenant_id
        self.data = {"slug": doc_id, "status": status, "summary": "a skill"}
        self.updated_at = datetime.now(timezone.utc)


def _patch_flag(monkeypatch, enabled: bool):
    """Force the opt-in check to a fixed bool. Patches BOTH the lenient
    ``_skills_factory_enabled`` (read/query/search/delete gates) and the
    strict ``_skills_factory_flag`` (query's explicit-status rejection
    path), so a test's ``enabled`` is honored consistently across both."""
    monkeypatch.setattr(mcp_server, "_skills_factory_enabled", _async_return(enabled))
    monkeypatch.setattr(mcp_server, "_skills_factory_flag", _async_return(enabled))


def _patch_sf_settings(monkeypatch, *, enabled: bool = True, **caps):
    """Stub the merged-settings lookup the op=write path makes. The write
    path now derives BOTH the opt-in flag and the byte caps from this one
    ``get_settings_for_display`` call (no separate ``_skills_factory_enabled``
    read), so tests set ``skills_factory.enabled`` here. Extra kwargs land
    as ``skills_factory`` leaves (e.g. ``description_max_bytes=None``)."""
    sf: dict = {"enabled": enabled}
    sf.update(caps)
    monkeypatch.setattr(
        "core_api.services.organization_settings.get_settings_for_display",
        _async_return({"skills_factory": sf}),
    )


def _valid_skill_data(**overrides):
    """A minimal SF-002-valid agent-direct skills write payload.

    Carries every REQUIRED_TOP_LEVEL_KEY plus a clean body so the
    Sentinel pre-scan passes. Override individual fields per test."""
    data = {
        "name": "Forge X",
        "slug": "forge-x",
        "description": "what this skill does",
        "domain": "ops",
        "kind": "create",
        "source": "agent",
        "content": "# Forge X\n\nDo the thing safely.",
        "summary": "Use when doing the thing in ops.",
    }
    data.update(overrides)
    return data


# ── op=read ────────────────────────────────────────────────────────


async def test_skill_read_hides_non_active_when_flag_on(mcp_env, monkeypatch):
    _patch_flag(monkeypatch, True)
    monkeypatch.setattr(
        "core_api.repositories.document_repo.get_by_doc_id",
        _async_return(_SkillRow("forge/x", status="staged")),
    )
    out = await mcp_server.memclaw_doc(op="read", collection="skills", doc_id="forge/x")
    # Non-active skill → same "Not found" as a missing doc (no existence leak).
    assert "Not found: skills/forge/x" in strip_latency(out)


async def test_skill_read_returns_active_when_flag_on(mcp_env, monkeypatch):
    _patch_flag(monkeypatch, True)
    monkeypatch.setattr(
        "core_api.repositories.document_repo.get_by_doc_id",
        _async_return(_SkillRow("forge/x", status="active")),
    )
    out = await mcp_server.memclaw_doc(op="read", collection="skills", doc_id="forge/x")
    payload = parse_envelope(out)
    assert payload["doc_id"] == "forge/x"
    assert payload["data"]["status"] == "active"


async def test_skill_read_no_filter_when_flag_off(mcp_env, monkeypatch):
    # Non-opted-in tenant: byte-identical to pre-Skill-Factory — a
    # staged (or status-less) skill is still readable.
    _patch_flag(monkeypatch, False)
    monkeypatch.setattr(
        "core_api.repositories.document_repo.get_by_doc_id",
        _async_return(_SkillRow("forge/x", status="staged")),
    )
    out = await mcp_server.memclaw_doc(op="read", collection="skills", doc_id="forge/x")
    payload = parse_envelope(out)
    assert payload["doc_id"] == "forge/x"


# ── op=query ───────────────────────────────────────────────────────


async def test_skill_query_rejects_explicit_non_active_status_when_flag_on(
    mcp_env, monkeypatch
):
    # An explicit non-active status is REJECTED (not silently rewritten
    # to active — that would mislead the caller into thinking they
    # queried 'staged'). Points them to the Inbox API.
    _patch_flag(monkeypatch, True)
    called = {"query": False}

    async def fake_query(db, **kwargs):  # noqa: ARG001
        called["query"] = True
        return []

    monkeypatch.setattr("core_api.repositories.document_repo.query", fake_query)
    out = await mcp_server.memclaw_doc(
        op="query", collection="skills", where={"status": "staged"}
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"
    assert "skills-inbox" in payload["error"]["message"].lower()
    assert called["query"] is False


async def test_skill_query_scopes_to_active_when_no_status_when_flag_on(
    mcp_env, monkeypatch
):
    # The common case — no status supplied — is transparently scoped to
    # 'active' (no error), so an agent's plain skills query only ever
    # sees live skills.
    _patch_flag(monkeypatch, True)
    captured: dict = {}

    async def fake_query(db, **kwargs):  # noqa: ARG001
        captured.update(kwargs)
        return []

    monkeypatch.setattr("core_api.repositories.document_repo.query", fake_query)
    await mcp_server.memclaw_doc(op="query", collection="skills", where={"domain": "ops"})
    assert captured["where"]["status"] == "active"
    assert captured["where"]["domain"] == "ops"


async def test_skill_query_no_filter_when_flag_off(mcp_env, monkeypatch):
    _patch_flag(monkeypatch, False)
    captured: dict = {}

    async def fake_query(db, **kwargs):  # noqa: ARG001
        captured.update(kwargs)
        return []

    monkeypatch.setattr("core_api.repositories.document_repo.query", fake_query)
    await mcp_server.memclaw_doc(
        op="query", collection="skills", where={"status": "staged"}
    )
    # Genuinely not opted in → caller's where passes through untouched
    # (legacy), NOT the confusing Inbox-API rejection.
    assert captured["where"] == {"status": "staged"}


async def test_skill_query_explicit_status_fails_closed_on_settings_error(
    mcp_env, monkeypatch
):
    # An explicit non-active status query is the security-sensitive
    # rejection path: a settings outage must fail CLOSED (INTERNAL_ERROR),
    # NOT emit the Inbox-API pointer to a tenant we can't confirm opted
    # in. Uses the strict _skills_factory_flag.
    async def _boom(*_a, **_k):
        raise RuntimeError("settings store down")

    monkeypatch.setattr(mcp_server, "_skills_factory_flag", _boom)
    called = {"query": False}

    async def fake_query(db, **kwargs):  # noqa: ARG001
        called["query"] = True
        return []

    monkeypatch.setattr("core_api.repositories.document_repo.query", fake_query)
    out = await mcp_server.memclaw_doc(
        op="query", collection="skills", where={"status": "staged"}
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert called["query"] is False


async def test_non_skills_query_unaffected_by_flag(mcp_env, monkeypatch):
    # The active-only policy is skills-only — a customers query must not
    # get a status filter injected even when the flag is on.
    _patch_flag(monkeypatch, True)
    captured: dict = {}

    async def fake_query(db, **kwargs):  # noqa: ARG001
        captured.update(kwargs)
        return []

    monkeypatch.setattr("core_api.repositories.document_repo.query", fake_query)
    await mcp_server.memclaw_doc(
        op="query", collection="customers", where={"plan": "biz"}
    )
    assert "status" not in captured["where"]


# ── op=search (scoped) ─────────────────────────────────────────────


async def test_skill_search_scoped_passes_active_status_when_flag_on(
    mcp_env, monkeypatch
):
    _patch_flag(monkeypatch, True)
    captured: dict = {}

    async def fake_search(db, **kwargs):  # noqa: ARG001
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    monkeypatch.setattr("core_api.repositories.document_repo.search", fake_search)
    await mcp_server.memclaw_doc(
        op="search", collection="skills", query="deploy eu-west"
    )
    assert captured["status"] == "active"


async def test_skill_search_scoped_no_status_when_flag_off(mcp_env, monkeypatch):
    _patch_flag(monkeypatch, False)
    captured: dict = {}

    async def fake_search(db, **kwargs):  # noqa: ARG001
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    monkeypatch.setattr("core_api.repositories.document_repo.search", fake_search)
    await mcp_server.memclaw_doc(
        op="search", collection="skills", query="deploy eu-west"
    )
    assert captured["status"] is None


# ── op=search (broad / cross-collection) ───────────────────────────


async def test_skill_search_broad_drops_non_active_when_flag_on(mcp_env, monkeypatch):
    _patch_flag(monkeypatch, True)
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    pairs = [
        (_DocRow("acme", collection="customers"), 0.9),  # non-skill: always kept
        (_SkillRow("forge/active", status="active"), 0.8),  # kept
        (_SkillRow("forge/staged", status="staged"), 0.7),  # DROPPED
    ]
    monkeypatch.setattr(
        "core_api.repositories.document_repo.search", _async_return(pairs)
    )
    out = await mcp_server.memclaw_doc(op="search", query="anything")
    payload = parse_envelope(out)
    returned = {(r["collection"], r["doc_id"]) for r in payload["results"]}
    assert ("customers", "acme") in returned
    assert ("skills", "forge/active") in returned
    assert ("skills", "forge/staged") not in returned  # in-flight skill never leaks


async def test_skill_search_broad_no_filter_when_flag_off(mcp_env, monkeypatch):
    _patch_flag(monkeypatch, False)
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    pairs = [(_SkillRow("forge/staged", status="staged"), 0.7)]
    monkeypatch.setattr(
        "core_api.repositories.document_repo.search", _async_return(pairs)
    )
    out = await mcp_server.memclaw_doc(op="search", query="anything")
    payload = parse_envelope(out)
    # Flag off → no broad post-filter; the staged skill surfaces as before.
    assert payload["results"][0]["doc_id"] == "forge/staged"


# ── op=write (self-promotion guard) ────────────────────────────────


async def test_skill_write_rejects_caller_active_status_when_flag_on(mcp_env, monkeypatch):
    # The MCP write path now runs the SF-002 lifecycle validator. An
    # agent-direct write carries is_admin=False, so a caller-supplied
    # status='active' hits ADMIN_ONLY_STATUSES and 403s — this is what
    # closes self-promotion past review.
    _patch_flag(monkeypatch, True)
    _patch_sf_settings(monkeypatch)
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    called = {"upsert": False}

    async def fake_upsert(db, **kwargs):  # noqa: ARG001
        called["upsert"] = True
        return _UpsertRow(xmax=0)

    monkeypatch.setattr(
        "core_api.repositories.document_repo.upsert_returning_xmax", fake_upsert
    )
    out = await mcp_server.memclaw_doc(
        op="write",
        collection="skills",
        doc_id="forge-x",
        data=_valid_skill_data(status="active"),
    )
    payload = parse_envelope(out)
    # Validator raises HTTPException(403) → outer handler maps to FORBIDDEN.
    assert payload["error"]["code"] == "FORBIDDEN"
    assert "admin" in payload["error"]["message"].lower()
    # The write must NOT have reached the DB.
    assert called["upsert"] is False


async def test_skill_write_rejects_forge_source_when_flag_on(mcp_env, monkeypatch):
    # source='forge' is reserved for the internal lifecycle worker.
    # An MCP caller is never is_internal_forge, so minting a forge-
    # sourced skill 403s (INTERNAL_ONLY_SOURCES). Agents use
    # source='agent'.
    _patch_flag(monkeypatch, True)
    _patch_sf_settings(monkeypatch)
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    called = {"upsert": False}

    async def fake_upsert(db, **kwargs):  # noqa: ARG001
        called["upsert"] = True
        return _UpsertRow(xmax=0)

    monkeypatch.setattr(
        "core_api.repositories.document_repo.upsert_returning_xmax", fake_upsert
    )
    out = await mcp_server.memclaw_doc(
        op="write",
        collection="skills",
        doc_id="forge-x",
        data=_valid_skill_data(source="forge"),
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "FORBIDDEN"
    assert called["upsert"] is False


async def test_skill_write_defaults_to_staged_when_flag_on(mcp_env, monkeypatch):
    # An agent-direct write with no status defaults to 'staged' (per
    # plan §4 / SF-002): it lands in the HITL inbox, NOT agent-visible,
    # until approved to 'active'. We capture the upserted data to assert
    # the validator normalized status → 'staged'.
    _patch_flag(monkeypatch, True)
    _patch_sf_settings(monkeypatch)
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    captured = {}

    async def fake_upsert(db, **kwargs):  # noqa: ARG001
        captured.update(kwargs)
        return _UpsertRow(xmax=0)

    monkeypatch.setattr(
        "core_api.repositories.document_repo.upsert_returning_xmax", fake_upsert
    )
    out = await mcp_server.memclaw_doc(
        op="write",
        collection="skills",
        doc_id="forge-x",
        data=_valid_skill_data(),  # no status
    )
    payload = parse_envelope(out)
    assert payload["ok"] is True
    # Validator-normalized data was passed through to the upsert.
    assert captured["data"]["status"] == "staged"
    assert captured["data"]["source"] == "agent"
    # content_hash is server-stamped, never trusted from the body.
    assert captured["data"]["content_hash"].startswith("sha256:")


async def test_skill_write_tolerates_misconfigured_byte_caps(mcp_env, monkeypatch):
    # A null / non-numeric per-tenant byte cap is a realistic admin
    # misconfiguration. It must degrade to the documented default via
    # _safe_int, not crash the write path with an opaque INTERNAL_ERROR.
    _patch_flag(monkeypatch, True)
    # enabled=True so the validator actually RUNS (and exercises
    # _safe_int on the null / non-numeric caps).
    _patch_sf_settings(
        monkeypatch, enabled=True, description_max_bytes=None, body_max_bytes="auto"
    )
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    monkeypatch.setattr(
        "core_api.repositories.document_repo.upsert_returning_xmax",
        _async_return(_UpsertRow(xmax=0)),
    )
    out = await mcp_server.memclaw_doc(
        op="write",
        collection="skills",
        doc_id="forge-x",
        data=_valid_skill_data(),
    )
    payload = parse_envelope(out)
    assert payload["ok"] is True


async def test_skill_write_fails_closed_on_settings_error(mcp_env, monkeypatch):
    # The settings read gates whether the lifecycle validator runs. If it
    # raises, the write must ABORT (INTERNAL_ERROR) — never fall through
    # and upsert an unvalidated skill.
    async def _boom(*_a, **_k):
        raise RuntimeError("settings store down")

    monkeypatch.setattr(
        "core_api.services.organization_settings.get_settings_for_display", _boom
    )
    called = {"upsert": False}

    async def fake_upsert(db, **kwargs):  # noqa: ARG001
        called["upsert"] = True
        return _UpsertRow(xmax=0)

    monkeypatch.setattr(
        "core_api.repositories.document_repo.upsert_returning_xmax", fake_upsert
    )
    out = await mcp_server.memclaw_doc(
        op="write", collection="skills", doc_id="forge-x", data=_valid_skill_data()
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert called["upsert"] is False


async def test_skill_update_write_fails_closed_on_live_doc_fetch_error(
    mcp_env, monkeypatch
):
    # A kind='update' write fetches the live skill for the validator's
    # hash-binding check. A transient storage error there must ABORT with
    # a curated INTERNAL_ERROR (not leak the raw exception), and never
    # reach the upsert.
    _patch_sf_settings(monkeypatch, enabled=True)
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )

    class _BoomClient:
        async def get_document(self, **_kwargs):
            raise RuntimeError("storage down")

    monkeypatch.setattr(mcp_server, "get_storage_client", lambda: _BoomClient())
    called = {"upsert": False}

    async def fake_upsert(db, **kwargs):  # noqa: ARG001
        called["upsert"] = True
        return _UpsertRow(xmax=0)

    monkeypatch.setattr(
        "core_api.repositories.document_repo.upsert_returning_xmax", fake_upsert
    )
    out = await mcp_server.memclaw_doc(
        op="write",
        collection="skills",
        doc_id="forge-x",
        data=_valid_skill_data(
            kind="update", target={"target_content_hash": "sha256:abc"}
        ),
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert called["upsert"] is False


async def test_skill_write_status_allowed_when_flag_off(mcp_env, monkeypatch):
    # Non-opted-in tenant: validator is skipped entirely, legacy
    # behavior — a minimal body with a caller-set status passes through
    # unchanged (byte-identical to pre-Skill-Factory behavior).
    _patch_flag(monkeypatch, False)
    _patch_sf_settings(monkeypatch, enabled=False)
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    captured = {}

    async def fake_upsert(db, **kwargs):  # noqa: ARG001
        captured.update(kwargs)
        return _UpsertRow(xmax=0)

    monkeypatch.setattr(
        "core_api.repositories.document_repo.upsert_returning_xmax", fake_upsert
    )
    out = await mcp_server.memclaw_doc(
        op="write",
        collection="skills",
        doc_id="forge-x",
        data={"slug": "forge-x", "summary": "s", "status": "active"},
    )
    payload = parse_envelope(out)
    assert payload["ok"] is True
    # Untouched: no validator ran, so status stays exactly as supplied.
    assert captured["data"]["status"] == "active"


# ── op=delete (active-only existence gate) ─────────────────────────


async def test_skill_delete_hides_non_active_when_flag_on(mcp_env, monkeypatch):
    # The status guard is folded into the DELETE's WHERE (atomic, no
    # pre-fetch). A staged skill matches zero rows → the DELETE returns
    # no id → the SAME generic JSON not-found a MISSING doc returns, so
    # a non-active skill is byte-for-byte indistinguishable from a
    # missing one (no existence leak) and the response is valid JSON.
    _patch_flag(monkeypatch, True)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None  # status=active matched nothing
    mcp_env["db"].execute.return_value = result_mock
    out = await mcp_server.memclaw_doc(
        op="delete", collection="skills", doc_id="forge/x"
    )
    payload = parse_envelope(out)  # must be valid JSON
    assert payload["error"] == "Document 'forge/x' not found in collection 'skills'"


async def test_skill_delete_allows_active_when_flag_on(mcp_env, monkeypatch):
    from uuid import uuid4

    # An active skill matches the WHERE status='active' guard → one row
    # deleted, returns its id.
    _patch_flag(monkeypatch, True)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = uuid4()
    mcp_env["db"].execute.return_value = result_mock
    out = await mcp_server.memclaw_doc(
        op="delete", collection="skills", doc_id="forge/x"
    )
    payload = parse_envelope(out)
    assert payload["deleted"] is True


async def test_skill_delete_fails_closed_on_settings_error(mcp_env, monkeypatch):
    # The status guard is a security gate: a settings-lookup failure must
    # ABORT the delete (INTERNAL_ERROR), not fall through to an
    # unguarded DELETE (which would let an agent delete a non-active
    # skill during an outage). Uses the strict _skills_factory_flag.
    async def _boom(*_a, **_k):
        raise RuntimeError("settings store down")

    monkeypatch.setattr(mcp_server, "_skills_factory_flag", _boom)
    out = await mcp_server.memclaw_doc(
        op="delete", collection="skills", doc_id="forge/x"
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    # The DELETE must never have run.
    mcp_env["db"].execute.assert_not_called()


# ── Cross-tenant leak: a sibling tenant's non-active skill must never
# surface, even when the CALLER's own tenant has not opted in ──────────


async def test_skill_read_hides_cross_tenant_non_active_even_when_caller_off(
    mcp_env, monkeypatch
):
    # Caller tenant ('test-tenant') is NOT opted in, but reads a sibling
    # tenant's staged skill via cross-tenant credentials. The owning
    # tenant's in-flight skill must stay hidden regardless of the
    # caller's flag.
    _patch_flag(monkeypatch, False)
    monkeypatch.setattr(
        mcp_server, "_get_readable_tenants", lambda: ["test-tenant", "sibling"]
    )
    monkeypatch.setattr(
        "core_api.repositories.document_repo.get_by_doc_id",
        _async_return(_SkillRow("forge/x", status="staged", tenant_id="sibling")),
    )
    out = await mcp_server.memclaw_doc(op="read", collection="skills", doc_id="forge/x")
    assert "Not found: skills/forge/x" in strip_latency(out)


async def test_skill_query_drops_cross_tenant_non_active_when_caller_off(
    mcp_env, monkeypatch
):
    _patch_flag(monkeypatch, False)
    monkeypatch.setattr(
        mcp_server, "_get_readable_tenants", lambda: ["test-tenant", "sibling"]
    )
    rows = [
        _SkillRow("own/active", status="active", tenant_id="test-tenant"),
        _SkillRow("own/staged", status="staged", tenant_id="test-tenant"),  # kept: caller off, same tenant
        _SkillRow("sib/staged", status="staged", tenant_id="sibling"),  # dropped: cross-tenant non-active
        _SkillRow("sib/active", status="active", tenant_id="sibling"),  # kept: active
    ]
    monkeypatch.setattr(
        "core_api.repositories.document_repo.query", _async_return(rows)
    )
    out = await mcp_server.memclaw_doc(op="query", collection="skills")
    payload = parse_envelope(out)
    ids = {r["doc_id"] for r in payload["results"]}
    assert "sib/staged" not in ids  # cross-tenant non-active leaked → blocked
    assert ids == {"own/active", "own/staged", "sib/active"}


async def test_skill_search_drops_cross_tenant_non_active_when_caller_off(
    mcp_env, monkeypatch
):
    _patch_flag(monkeypatch, False)
    monkeypatch.setattr(
        mcp_server, "_get_readable_tenants", lambda: ["test-tenant", "sibling"]
    )
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    pairs = [
        (_SkillRow("sib/staged", status="staged", tenant_id="sibling"), 0.9),
        (_SkillRow("sib/active", status="active", tenant_id="sibling"), 0.8),
        (_SkillRow("own/staged", status="staged", tenant_id="test-tenant"), 0.7),
    ]
    monkeypatch.setattr(
        "core_api.repositories.document_repo.search", _async_return(pairs)
    )
    # Broad search (collection=None) over the readable set.
    out = await mcp_server.memclaw_doc(op="search", query="deploy")
    payload = parse_envelope(out)
    ids = {r["doc_id"] for r in payload["results"]}
    assert "sib/staged" not in ids  # cross-tenant non-active dropped
    assert ids == {"sib/active", "own/staged"}  # own staged stays (caller off)


# ── op=list_collections (active-only count for skills) ─────────────────


async def test_list_collections_skill_count_active_only_when_flag_on(
    mcp_env, monkeypatch
):
    _patch_flag(monkeypatch, True)
    monkeypatch.setattr(
        "core_api.repositories.document_repo.list_collections",
        _async_return([("customers", 5), ("skills", 9)]),
    )
    # The active-only recount issues one COUNT via db.execute.
    result_mock = MagicMock()
    result_mock.scalar_one.return_value = 3  # only 3 of the 9 skills are active
    mcp_env["db"].execute.return_value = result_mock
    out = await mcp_server.memclaw_doc(op="list_collections")
    payload = parse_envelope(out)
    counts = {c["name"]: c["count"] for c in payload["collections"]}
    assert counts["skills"] == 3  # corrected to active-only
    assert counts["customers"] == 5  # non-skills untouched


async def test_list_collections_skill_count_unchanged_when_flag_off(
    mcp_env, monkeypatch
):
    _patch_flag(monkeypatch, False)
    monkeypatch.setattr(
        "core_api.repositories.document_repo.list_collections",
        _async_return([("skills", 9)]),
    )
    out = await mcp_server.memclaw_doc(op="list_collections")
    payload = parse_envelope(out)
    counts = {c["name"]: c["count"] for c in payload["collections"]}
    assert counts["skills"] == 9  # legacy: full count, no recount


# ── op=write (case-variant status key) ─────────────────────────────────


async def test_skill_write_rejects_case_variant_status_key_when_flag_on(
    mcp_env, monkeypatch
):
    # A 'STATUS' key can't self-promote (gates read lowercase
    # data->>'status'), but it would persist as a confusing shadow
    # field. Reject it outright.
    _patch_flag(monkeypatch, True)
    _patch_sf_settings(monkeypatch)
    called = {"upsert": False}

    async def fake_upsert(db, **kwargs):  # noqa: ARG001
        called["upsert"] = True
        return _UpsertRow(xmax=0)

    monkeypatch.setattr(
        "core_api.repositories.document_repo.upsert_returning_xmax", fake_upsert
    )
    out = await mcp_server.memclaw_doc(
        op="write",
        collection="skills",
        doc_id="forge-x",
        data=_valid_skill_data(STATUS="active"),
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"
    assert "status" in payload["error"]["message"].lower()
    assert called["upsert"] is False


async def test_safe_int_degrades_bool_and_garbage_to_default():
    # bool is an int subclass — int(True)==1 — so it must be rejected
    # explicitly, alongside null / non-numeric strings.
    assert mcp_server._safe_int(True, 160) == 160
    assert mcp_server._safe_int(False, 160) == 160
    assert mcp_server._safe_int(None, 160) == 160
    assert mcp_server._safe_int("auto", 160) == 160
    assert mcp_server._safe_int([], 160) == 160
    # Valid values still pass through (ints and numeric strings).
    assert mcp_server._safe_int(40_000, 160) == 40_000
    assert mcp_server._safe_int("4096", 160) == 4096


async def test_skills_factory_enabled_fails_closed_on_settings_error(mcp_env, monkeypatch):
    # The read-path helper must fail CLOSED: if the strict flag lookup
    # raises, assume enabled (True) so the active-only filter is applied
    # and non-active skills can't leak during a settings outage.
    async def _boom(*_a, **_k):
        raise RuntimeError("settings store down")

    monkeypatch.setattr(mcp_server, "_skills_factory_flag", _boom)
    result = await mcp_server._skills_factory_enabled(mcp_env["db"], "test-tenant")
    assert result is True


async def test_skill_read_hides_non_active_when_flag_lookup_errors(mcp_env, monkeypatch):
    # End-to-end: a settings outage during a skill read filters (hides
    # the non-active skill), it does not 500 or leak.
    async def _boom(*_a, **_k):
        raise RuntimeError("settings store down")

    monkeypatch.setattr(mcp_server, "_skills_factory_flag", _boom)
    monkeypatch.setattr(
        "core_api.repositories.document_repo.get_by_doc_id",
        _async_return(_SkillRow("forge/x", status="staged")),
    )
    out = await mcp_server.memclaw_doc(op="read", collection="skills", doc_id="forge/x")
    assert "Not found: skills/forge/x" in strip_latency(out)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _async_return(value):
    async def _fn(*args, **kwargs):  # noqa: ARG001
        return value

    return _fn
