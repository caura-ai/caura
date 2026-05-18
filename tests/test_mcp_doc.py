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
    assert "embedding provider returned no vector" in strip_latency(out)


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
    assert "embedding provider returned no vector" in strip_latency(out)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _async_return(value):
    async def _fn(*args, **kwargs):  # noqa: ARG001
        return value

    return _fn
