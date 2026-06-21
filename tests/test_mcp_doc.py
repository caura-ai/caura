"""Unit tests for ``memclaw_doc`` (op: write | read | query | delete).

Covers:
- Unknown op → ``INVALID_ARGUMENTS`` envelope.
- Per-op required-parameter validation (doc_id, data).
- Happy paths for all four ops (action/payload/count fields).
- ``op=read`` not-found → "Not found:" text.
- ``op=delete`` not-found → structured error envelope.

Fix 2 Phase 4 routed ``memclaw_doc`` through the core-storage-api HTTP
client (``get_storage_client()``), so these tests stub the storage client
(``stub_storage_client``) and assert on the new dict-shaped payloads /
single-positional-dict call args, rather than the old ``document_repo.*``
ORM-row / tuple shapes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core_api import mcp_server
from core_api.constants import VECTOR_DIM
from tests._mcp_test_helpers import parse_envelope, strip_latency, stub_storage_client

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _doc(doc_id: str = "acme", collection: str = "customers", **extra) -> dict:
    """A storage-client document dict (Fix 2 Phase 4 shape)."""
    doc = {
        "collection": collection,
        "doc_id": doc_id,
        "tenant_id": "test-tenant",
        "data": {"plan": "business"},
        "updated_at": datetime.now(timezone.utc),
    }
    doc.update(extra)
    return doc


def _search_hit(
    doc_id: str, similarity: float, collection: str = "customers", **extra
) -> dict:
    """A storage-client search result: a doc dict with an inline
    ``similarity`` float (vs the repo's ``(Document, sim)`` tuples)."""
    return _doc(doc_id, collection=collection, similarity=similarity, **extra)


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
    stub_storage_client(monkeypatch, upsert_document_xmax={"xmax": 0})
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
    stub_storage_client(monkeypatch, upsert_document_xmax={"xmax": 42})
    out = await mcp_server.memclaw_doc(
        op="write", collection="customers", doc_id="acme", data={"plan": "pro"}
    )
    payload = parse_envelope(out)
    assert payload["action"] == "updated"


async def test_doc_read_missing_doc_id(mcp_env):
    out = await mcp_server.memclaw_doc(op="read", collection="customers")
    assert "op=read requires 'doc_id'" in strip_latency(out)


async def test_doc_read_not_found(mcp_env, monkeypatch):
    stub_storage_client(monkeypatch, get_document=None)
    out = await mcp_server.memclaw_doc(
        op="read", collection="customers", doc_id="ghost"
    )
    assert "Not found: customers/ghost" in strip_latency(out)


async def test_doc_read_happy_path(mcp_env, monkeypatch):
    stub_storage_client(monkeypatch, get_document=_doc("acme"))
    out = await mcp_server.memclaw_doc(op="read", collection="customers", doc_id="acme")
    payload = parse_envelope(out)
    assert payload["doc_id"] == "acme"
    assert payload["data"] == {"plan": "business"}


async def test_doc_query_happy_path(mcp_env, monkeypatch):
    rows = [_doc("acme"), _doc("initech")]
    stub_storage_client(monkeypatch, query_documents=rows)
    out = await mcp_server.memclaw_doc(
        op="query", collection="customers", where={"plan": "business"}
    )
    payload = parse_envelope(out)
    assert payload["count"] == 2
    assert payload["collection"] == "customers"
    assert [r["doc_id"] for r in payload["results"]] == ["acme", "initech"]


async def test_doc_query_where_defaults_to_empty_dict(mcp_env, monkeypatch):
    sc = stub_storage_client(monkeypatch, query_documents=[])
    await mcp_server.memclaw_doc(op="query", collection="customers")
    assert sc.query_documents.await_args.args[0]["where"] == {}


async def test_doc_delete_missing_doc_id(mcp_env):
    out = await mcp_server.memclaw_doc(op="delete", collection="customers")
    assert "op=delete requires 'doc_id'" in strip_latency(out)


async def test_doc_delete_not_found_envelope(mcp_env, monkeypatch):
    """A storage delete that matched nothing returns False → the handler
    emits a ``{"error": "…"}`` JSON blob."""
    sc = stub_storage_client(monkeypatch, delete_document=False)
    out = await mcp_server.memclaw_doc(
        op="delete", collection="customers", doc_id="ghost"
    )
    payload = parse_envelope(out)
    assert "not found" in payload["error"].lower()
    assert "ghost" in payload["error"]
    sc.delete_document.assert_awaited_once()


async def test_doc_delete_happy_path(mcp_env, monkeypatch):
    sc = stub_storage_client(monkeypatch, delete_document=True)
    out = await mcp_server.memclaw_doc(
        op="delete", collection="customers", doc_id="acme"
    )
    payload = parse_envelope(out)
    assert payload["ok"] is True
    assert payload["deleted"] is True
    assert payload["doc_id"] == "acme"
    sc.delete_document.assert_awaited_once()


async def test_doc_auth_failure_shortcircuits(monkeypatch):
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: mcp_server._AUTH_ERROR)
    out = await mcp_server.memclaw_doc(op="read", collection="c", doc_id="d")
    assert out == mcp_server._AUTH_ERROR


# ---------------------------------------------------------------------------
# op=list_collections
# ---------------------------------------------------------------------------


async def test_doc_list_collections_happy_path(mcp_env, monkeypatch):
    """Returns each collection with its document count."""
    stub_storage_client(
        monkeypatch,
        list_document_collections={
            "collections": [
                {"name": "customers", "count": 3},
                {"name": "onboarding_guides", "count": 1},
                {"name": "proposals", "count": 2},
            ]
        },
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
    stub_storage_client(monkeypatch, list_document_collections={"collections": []})
    out = await mcp_server.memclaw_doc(op="list_collections")
    payload = parse_envelope(out)
    assert payload["collections"] == []
    assert payload["count"] == 0


async def test_doc_list_collections_does_not_require_collection(mcp_env, monkeypatch):
    """Unlike every other op, list_collections has no required params — the
    whole point is to discover collection names when you don't know them yet.
    """
    stub_storage_client(monkeypatch, list_document_collections={"collections": []})
    out = await mcp_server.memclaw_doc(op="list_collections")
    payload = parse_envelope(out)
    assert "error" not in payload


async def test_doc_list_collections_passes_fleet_id_filter(mcp_env, monkeypatch):
    """fleet_id scopes the count; not a mandatory param."""
    sc = stub_storage_client(
        monkeypatch,
        list_document_collections={"collections": [{"name": "customers", "count": 1}]},
    )
    await mcp_server.memclaw_doc(op="list_collections", fleet_id="caura-rnd-fleet")
    assert (
        sc.list_document_collections.await_args.kwargs["fleet_id"] == "caura-rnd-fleet"
    )


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
    forwards the vector to the storage client. Response reports indexed=True."""
    captured: dict = {}

    async def fake_embed(text):
        captured["embed_text"] = text
        return [0.1] * VECTOR_DIM

    sc = stub_storage_client(monkeypatch, upsert_document_xmax={"xmax": 0})
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
    # The embedding is forwarded in the dict passed to upsert_document_xmax.
    sent = sc.upsert_document_xmax.await_args.args[0]
    assert len(sent["embedding"]) == VECTOR_DIM


async def test_doc_write_no_summary_stores_unindexed(mcp_env, monkeypatch):
    """Non-skills writes without data["summary"] persist without an
    embedding — the "I don't need semantic search" path stays open."""
    called = {"hit": False}

    async def should_not_embed(text):  # noqa: ARG001
        called["hit"] = True
        return [0.0] * VECTOR_DIM

    stub_storage_client(monkeypatch, upsert_document_xmax={"xmax": 0})
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
    storage client, not reject the call."""
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    sc = stub_storage_client(monkeypatch, search_documents_vector=[])

    out = await mcp_server.memclaw_doc(op="search", query="onboarding")
    payload = parse_envelope(out)
    # No 422 — broad search is a legitimate call
    assert "error" not in payload
    assert sc.search_documents_vector.await_args.args[0]["collection"] is None


async def test_doc_search_broad_results_include_per_row_collection(
    mcp_env, monkeypatch
):
    """Each result row must include its own `collection` so the caller can
    follow up with op=read across mixed collections."""
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    hits = [
        _search_hit("acme", 0.9, collection="customers"),
        _search_hit("guide-1", 0.6, collection="onboarding_guides"),
    ]
    stub_storage_client(monkeypatch, search_documents_vector=hits)
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
    """Happy path: embedding → storage search → results sorted by similarity."""
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    hits = [_search_hit("acme", 0.92), _search_hit("initech", 0.81)]
    stub_storage_client(monkeypatch, search_documents_vector=hits)
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
    stub_storage_client(monkeypatch, search_documents_vector=[])
    out = await mcp_server.memclaw_doc(op="search", collection="c", query="anything")
    payload = parse_envelope(out)
    assert payload["count"] == 0
    assert payload["results"] == []


async def test_doc_search_top_k_capped_at_50(mcp_env, monkeypatch):
    """top_k above 50 is capped server-side."""
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    sc = stub_storage_client(monkeypatch, search_documents_vector=[])

    await mcp_server.memclaw_doc(op="search", collection="c", query="q", top_k=9999)
    assert sc.search_documents_vector.await_args.args[0]["top_k"] == 50


async def test_doc_search_embedding_provider_failure_aborts(mcp_env, monkeypatch):
    """Provider failure → no search attempt, caller sees a clear error."""
    monkeypatch.setattr("common.embedding.get_embedding", _async_return(None))
    out = await mcp_server.memclaw_doc(op="search", collection="c", query="anything")
    assert "embedding provider returned no vector" in strip_latency(out).lower()


# ---------------------------------------------------------------------------
# Read-op widening: ``_readable_tenant_ids_var`` reaches the storage call
# (audit T1)
# ---------------------------------------------------------------------------
#
# Cross-tenant credentials surface as a non-empty
# ``_readable_tenant_ids_var``; the tool reads it via
# ``_get_readable_tenants()`` and passes the list as ``readable_tenant_ids``
# to whichever storage method backs the requested op:
#
#   list_collections → sc.list_document_collections   (kwarg)
#   read             → sc.get_document                 (kwarg)
#   query            → sc.query_documents              (dict key)
#   search           → sc.search_documents_vector      (dict key)
#
# T1 locks in the wiring: a regression that silently drops the list on
# any of those four paths would fail the corresponding test below.


async def test_doc_list_collections_passes_readable_tenants(mcp_env, monkeypatch):
    """``op=list_collections`` widens to the readable set when the
    caller's credential is cross-tenant."""
    monkeypatch.setattr(
        mcp_server, "_get_readable_tenants", lambda: ["home", "sibling"]
    )
    sc = stub_storage_client(monkeypatch, list_document_collections={"collections": []})
    await mcp_server.memclaw_doc(op="list_collections")
    assert sc.list_document_collections.await_args.kwargs["readable_tenant_ids"] == [
        "home",
        "sibling",
    ]


async def test_doc_list_collections_single_tenant_passes_none(mcp_env, monkeypatch):
    """Single-tenant credential: ``_get_readable_tenants()`` returns
    ``[]`` → the tool collapses it to ``None`` before calling storage
    (so the storage single-tenant fast path runs)."""
    monkeypatch.setattr(mcp_server, "_get_readable_tenants", lambda: [])
    sc = stub_storage_client(monkeypatch, list_document_collections={"collections": []})
    await mcp_server.memclaw_doc(op="list_collections")
    assert sc.list_document_collections.await_args.kwargs["readable_tenant_ids"] is None


async def test_doc_read_passes_readable_tenants(mcp_env, monkeypatch):
    """``op=read`` widens via ``readable_tenant_ids``. Storage can
    then resolve a doc that lives in a sibling tenant when the caller
    is authorized to read from it."""
    monkeypatch.setattr(
        mcp_server, "_get_readable_tenants", lambda: ["home", "sibling"]
    )
    sc = stub_storage_client(monkeypatch, get_document=_doc())
    await mcp_server.memclaw_doc(op="read", collection="customers", doc_id="acme")
    assert sc.get_document.await_args.kwargs["readable_tenant_ids"] == [
        "home",
        "sibling",
    ]


async def test_doc_query_passes_readable_tenants(mcp_env, monkeypatch):
    """``op=query`` (filter-by-data) widens the same way."""
    monkeypatch.setattr(
        mcp_server, "_get_readable_tenants", lambda: ["home", "sibling"]
    )
    sc = stub_storage_client(monkeypatch, query_documents=[])
    await mcp_server.memclaw_doc(op="query", collection="customers", where={"k": 1})
    assert sc.query_documents.await_args.args[0]["readable_tenant_ids"] == [
        "home",
        "sibling",
    ]


async def test_doc_search_passes_readable_tenants(mcp_env, monkeypatch):
    """``op=search`` (vector recall) widens the same way. The audit
    emission has its own assertion in
    ``tests/test_cross_tenant_audit_surfaces.py``; this test only
    confirms the wiring from the context var to the storage arg."""
    monkeypatch.setattr(
        mcp_server, "_get_readable_tenants", lambda: ["home", "sibling"]
    )
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    sc = stub_storage_client(monkeypatch, search_documents_vector=[])
    await mcp_server.memclaw_doc(op="search", query="hello")
    assert sc.search_documents_vector.await_args.args[0]["readable_tenant_ids"] == [
        "home",
        "sibling",
    ]


# ---------------------------------------------------------------------------
# Active-only skill discovery (MCP-direct delivery)
#
# The agent-facing memclaw_doc surface must expose only ``status='active'``
# skills to opted-in tenants — candidate / staged / quarantined skills are
# in-flight or blocked and must not surface. Gated on
# ``skills_factory.enabled``: a non-opted-in tenant's reads are unchanged.
# ---------------------------------------------------------------------------


def _skill_doc(doc_id: str, status: str, tenant_id: str = "test-tenant") -> dict:
    """A storage-client skills document dict."""
    return {
        "collection": "skills",
        "doc_id": doc_id,
        "tenant_id": tenant_id,
        "data": {"slug": doc_id, "status": status, "summary": "a skill"},
        "updated_at": datetime.now(timezone.utc),
    }


def _skill_hit(
    doc_id: str, status: str, similarity: float, tenant_id: str = "test-tenant"
) -> dict:
    """A storage-client skills search result (skill doc + inline similarity)."""
    return {**_skill_doc(doc_id, status, tenant_id), "similarity": similarity}


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
    as ``skills_factory`` leaves (e.g. ``description_max_bytes=None``).

    The handler calls the module-level ``mcp_server.get_settings_for_display``
    name, so patch that alias (patching the original
    ``core_api.services.organization_settings.get_settings_for_display`` would
    not rebind the name the handler resolves)."""
    sf: dict = {"enabled": enabled}
    sf.update(caps)
    monkeypatch.setattr(
        mcp_server,
        "get_settings_for_display",
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
    stub_storage_client(
        monkeypatch, get_document=_skill_doc("forge/x", status="staged")
    )
    out = await mcp_server.memclaw_doc(op="read", collection="skills", doc_id="forge/x")
    # Non-active skill → same "Not found" as a missing doc (no existence leak).
    assert "Not found: skills/forge/x" in strip_latency(out)


async def test_skill_read_returns_active_when_flag_on(mcp_env, monkeypatch):
    _patch_flag(monkeypatch, True)
    stub_storage_client(
        monkeypatch, get_document=_skill_doc("forge/x", status="active")
    )
    out = await mcp_server.memclaw_doc(op="read", collection="skills", doc_id="forge/x")
    payload = parse_envelope(out)
    assert payload["doc_id"] == "forge/x"
    assert payload["data"]["status"] == "active"


async def test_skill_read_no_filter_when_flag_off(mcp_env, monkeypatch):
    # Non-opted-in tenant: byte-identical to pre-Skill-Factory — a
    # staged (or status-less) skill is still readable.
    _patch_flag(monkeypatch, False)
    stub_storage_client(
        monkeypatch, get_document=_skill_doc("forge/x", status="staged")
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
    sc = stub_storage_client(monkeypatch, query_documents=[])
    out = await mcp_server.memclaw_doc(
        op="query", collection="skills", where={"status": "staged"}
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"
    assert "skills-inbox" in payload["error"]["message"].lower()
    sc.query_documents.assert_not_awaited()


async def test_skill_query_scopes_to_active_when_no_status_when_flag_on(
    mcp_env, monkeypatch
):
    # The common case — no status supplied — is transparently scoped to
    # 'active' (no error), so an agent's plain skills query only ever
    # sees live skills.
    _patch_flag(monkeypatch, True)
    sc = stub_storage_client(monkeypatch, query_documents=[])
    await mcp_server.memclaw_doc(
        op="query", collection="skills", where={"domain": "ops"}
    )
    sent = sc.query_documents.await_args.args[0]
    assert sent["where"]["status"] == "active"
    assert sent["where"]["domain"] == "ops"


async def test_skill_query_no_filter_when_flag_off(mcp_env, monkeypatch):
    _patch_flag(monkeypatch, False)
    sc = stub_storage_client(monkeypatch, query_documents=[])
    await mcp_server.memclaw_doc(
        op="query", collection="skills", where={"status": "staged"}
    )
    # Genuinely not opted in → caller's where passes through untouched
    # (legacy), NOT the confusing Inbox-API rejection.
    assert sc.query_documents.await_args.args[0]["where"] == {"status": "staged"}


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
    sc = stub_storage_client(monkeypatch, query_documents=[])
    out = await mcp_server.memclaw_doc(
        op="query", collection="skills", where={"status": "staged"}
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    sc.query_documents.assert_not_awaited()


async def test_non_skills_query_unaffected_by_flag(mcp_env, monkeypatch):
    # The active-only policy is skills-only — a customers query must not
    # get a status filter injected even when the flag is on.
    _patch_flag(monkeypatch, True)
    sc = stub_storage_client(monkeypatch, query_documents=[])
    await mcp_server.memclaw_doc(
        op="query", collection="customers", where={"plan": "biz"}
    )
    assert "status" not in sc.query_documents.await_args.args[0]["where"]


# ── op=search (scoped) ─────────────────────────────────────────────


async def test_skill_search_scoped_passes_active_status_when_flag_on(
    mcp_env, monkeypatch
):
    _patch_flag(monkeypatch, True)
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    sc = stub_storage_client(monkeypatch, search_documents_vector=[])
    await mcp_server.memclaw_doc(
        op="search", collection="skills", query="deploy eu-west"
    )
    assert sc.search_documents_vector.await_args.args[0]["status"] == "active"


async def test_skill_search_scoped_no_status_when_flag_off(mcp_env, monkeypatch):
    _patch_flag(monkeypatch, False)
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    sc = stub_storage_client(monkeypatch, search_documents_vector=[])
    await mcp_server.memclaw_doc(
        op="search", collection="skills", query="deploy eu-west"
    )
    assert sc.search_documents_vector.await_args.args[0]["status"] is None


# ── op=search (broad / cross-collection) ───────────────────────────


async def test_skill_search_broad_drops_non_active_when_flag_on(mcp_env, monkeypatch):
    _patch_flag(monkeypatch, True)
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    hits = [
        _search_hit("acme", 0.9, collection="customers"),  # non-skill: always kept
        _skill_hit("forge/active", status="active", similarity=0.8),  # kept
        _skill_hit("forge/staged", status="staged", similarity=0.7),  # DROPPED
    ]
    stub_storage_client(monkeypatch, search_documents_vector=hits)
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
    hits = [_skill_hit("forge/staged", status="staged", similarity=0.7)]
    stub_storage_client(monkeypatch, search_documents_vector=hits)
    out = await mcp_server.memclaw_doc(op="search", query="anything")
    payload = parse_envelope(out)
    # Flag off → no broad post-filter; the staged skill surfaces as before.
    assert payload["results"][0]["doc_id"] == "forge/staged"


# ── op=write (self-promotion guard) ────────────────────────────────


async def test_skill_write_rejects_caller_active_status_when_flag_on(
    mcp_env, monkeypatch
):
    # The MCP write path now runs the SF-002 lifecycle validator. An
    # agent-direct write carries is_admin=False, so a caller-supplied
    # status='active' hits ADMIN_ONLY_STATUSES and 403s — this is what
    # closes self-promotion past review.
    _patch_flag(monkeypatch, True)
    _patch_sf_settings(monkeypatch)
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    sc = stub_storage_client(monkeypatch, upsert_document_xmax={"xmax": 0})
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
    # The write must NOT have reached storage.
    sc.upsert_document_xmax.assert_not_awaited()


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
    sc = stub_storage_client(monkeypatch, upsert_document_xmax={"xmax": 0})
    out = await mcp_server.memclaw_doc(
        op="write",
        collection="skills",
        doc_id="forge-x",
        data=_valid_skill_data(source="forge"),
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "FORBIDDEN"
    sc.upsert_document_xmax.assert_not_awaited()


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
    sc = stub_storage_client(monkeypatch, upsert_document_xmax={"xmax": 0})
    out = await mcp_server.memclaw_doc(
        op="write",
        collection="skills",
        doc_id="forge-x",
        data=_valid_skill_data(),  # no status
    )
    payload = parse_envelope(out)
    assert payload["ok"] is True
    # Validator-normalized data was passed through to the upsert.
    sent_data = sc.upsert_document_xmax.await_args.args[0]["data"]
    assert sent_data["status"] == "staged"
    assert sent_data["source"] == "agent"
    # content_hash is server-stamped, never trusted from the body.
    assert sent_data["content_hash"].startswith("sha256:")


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
    stub_storage_client(monkeypatch, upsert_document_xmax={"xmax": 0})
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

    monkeypatch.setattr(mcp_server, "get_settings_for_display", _boom)
    sc = stub_storage_client(monkeypatch, upsert_document_xmax={"xmax": 0})
    out = await mcp_server.memclaw_doc(
        op="write", collection="skills", doc_id="forge-x", data=_valid_skill_data()
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    sc.upsert_document_xmax.assert_not_awaited()


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

    async def _boom_get_document(**_kwargs):
        raise RuntimeError("storage down")

    # The live-doc fetch (get_document) raises; the upsert must never run.
    sc = stub_storage_client(monkeypatch, upsert_document_xmax={"xmax": 0})
    sc.get_document.side_effect = _boom_get_document
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
    sc.upsert_document_xmax.assert_not_awaited()


async def test_skill_write_status_allowed_when_flag_off(mcp_env, monkeypatch):
    # Non-opted-in tenant: validator is skipped entirely, legacy
    # behavior — a minimal body with a caller-set status passes through
    # unchanged (byte-identical to pre-Skill-Factory behavior).
    _patch_flag(monkeypatch, False)
    _patch_sf_settings(monkeypatch, enabled=False)
    monkeypatch.setattr(
        "common.embedding.get_embedding", _async_return([0.1] * VECTOR_DIM)
    )
    sc = stub_storage_client(monkeypatch, upsert_document_xmax={"xmax": 0})
    out = await mcp_server.memclaw_doc(
        op="write",
        collection="skills",
        doc_id="forge-x",
        data={"slug": "forge-x", "summary": "s", "status": "active"},
    )
    payload = parse_envelope(out)
    assert payload["ok"] is True
    # Untouched: no validator ran, so status stays exactly as supplied.
    assert sc.upsert_document_xmax.await_args.args[0]["data"]["status"] == "active"


# ── op=delete (active-only existence gate) ─────────────────────────


async def test_skill_delete_hides_non_active_when_flag_on(mcp_env, monkeypatch):
    # The status guard is folded into the storage DELETE's WHERE
    # (atomic, via ``require_status``). A staged skill matches zero rows →
    # storage returns False → the SAME generic JSON not-found a MISSING
    # doc returns, so a non-active skill is byte-for-byte indistinguishable
    # from a missing one (no existence leak) and the response is valid JSON.
    _patch_flag(monkeypatch, True)
    stub_storage_client(
        monkeypatch, delete_document=False
    )  # status guard matched nothing
    out = await mcp_server.memclaw_doc(
        op="delete", collection="skills", doc_id="forge/x"
    )
    payload = parse_envelope(out)  # must be valid JSON
    assert payload["error"] == "Document 'forge/x' not found in collection 'skills'"


async def test_skill_delete_allows_active_when_flag_on(mcp_env, monkeypatch):
    # An active skill matches the require_status='active' guard → one row
    # deleted, storage returns True.
    _patch_flag(monkeypatch, True)
    stub_storage_client(monkeypatch, delete_document=True)
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
    sc = stub_storage_client(monkeypatch, delete_document=True)
    out = await mcp_server.memclaw_doc(
        op="delete", collection="skills", doc_id="forge/x"
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    # The DELETE must never have run.
    sc.delete_document.assert_not_awaited()


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
    stub_storage_client(
        monkeypatch,
        get_document=_skill_doc("forge/x", status="staged", tenant_id="sibling"),
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
        _skill_doc("own/active", status="active", tenant_id="test-tenant"),
        _skill_doc(
            "own/staged", status="staged", tenant_id="test-tenant"
        ),  # kept: caller off, same tenant
        _skill_doc(
            "sib/staged", status="staged", tenant_id="sibling"
        ),  # dropped: cross-tenant non-active
        _skill_doc("sib/active", status="active", tenant_id="sibling"),  # kept: active
    ]
    stub_storage_client(monkeypatch, query_documents=rows)
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
    hits = [
        _skill_hit("sib/staged", status="staged", similarity=0.9, tenant_id="sibling"),
        _skill_hit("sib/active", status="active", similarity=0.8, tenant_id="sibling"),
        _skill_hit(
            "own/staged", status="staged", similarity=0.7, tenant_id="test-tenant"
        ),
    ]
    stub_storage_client(monkeypatch, search_documents_vector=hits)
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
    # The active-only recount issues one COUNT via the storage client.
    stub_storage_client(
        monkeypatch,
        list_document_collections={
            "collections": [
                {"name": "customers", "count": 5},
                {"name": "skills", "count": 9},
            ]
        },
        document_count_in_collection=3,  # only 3 of the 9 skills are active
    )
    out = await mcp_server.memclaw_doc(op="list_collections")
    payload = parse_envelope(out)
    counts = {c["name"]: c["count"] for c in payload["collections"]}
    assert counts["skills"] == 3  # corrected to active-only
    assert counts["customers"] == 5  # non-skills untouched


async def test_list_collections_skill_count_unchanged_when_flag_off(
    mcp_env, monkeypatch
):
    _patch_flag(monkeypatch, False)
    stub_storage_client(
        monkeypatch,
        list_document_collections={"collections": [{"name": "skills", "count": 9}]},
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
    sc = stub_storage_client(monkeypatch, upsert_document_xmax={"xmax": 0})
    out = await mcp_server.memclaw_doc(
        op="write",
        collection="skills",
        doc_id="forge-x",
        data=_valid_skill_data(STATUS="active"),
    )
    payload = parse_envelope(out)
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"
    assert "status" in payload["error"]["message"].lower()
    sc.upsert_document_xmax.assert_not_awaited()


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


async def test_skills_factory_enabled_fails_closed_on_settings_error(
    mcp_env, monkeypatch
):
    # The read-path helper must fail CLOSED: if the strict flag lookup
    # raises, assume enabled (True) so the active-only filter is applied
    # and non-active skills can't leak during a settings outage.
    async def _boom(*_a, **_k):
        raise RuntimeError("settings store down")

    monkeypatch.setattr(mcp_server, "_skills_factory_flag", _boom)
    result = await mcp_server._skills_factory_enabled(mcp_env["db"], "test-tenant")
    assert result is True


async def test_skill_read_hides_non_active_when_flag_lookup_errors(
    mcp_env, monkeypatch
):
    # End-to-end: a settings outage during a skill read filters (hides
    # the non-active skill), it does not 500 or leak.
    async def _boom(*_a, **_k):
        raise RuntimeError("settings store down")

    monkeypatch.setattr(mcp_server, "_skills_factory_flag", _boom)
    stub_storage_client(
        monkeypatch, get_document=_skill_doc("forge/x", status="staged")
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
