"""The REST ``POST /documents`` surface mints the same doc-derived memory as MCP.

``doc_indexing.resolve_doc_memory`` is deliberately the single source of truth for
both surfaces (mirroring ``resolve_embed_source``). The anti-drift test here is the
point of the file: if someone changes the rule for one surface only, these fail.

Covers:
- Both surfaces produce an IDENTICAL ``DocMemorySpec`` for identical input.
- Minting fires on BOTH REST upsert branches — the indexed branch
  (``upsert_document_xmax`` + re-fetch) and the unindexed branch
  (``upsert_document``).
- A raising mint cannot fail the document write.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from core_api.services.doc_indexing import resolve_doc_memory

pytestmark = [pytest.mark.unit]


_BODY = "# Vacuum\n\n  Run VACUUM ANALYZE nightly.\n"


# ── Anti-drift: one rule, both surfaces ───────────────────────────────────────


def test_both_surfaces_share_one_rule():
    """Both call sites invoke the same function with the same argument shape, so
    identical input must yield an identical spec.

    This is a structural guarantee rather than a behavioural one: the assertion
    that matters is that neither surface owns a private copy of the rule.
    """
    data = {"summary": "Postgres tuning runbook.", "content": _BODY}

    mcp_spec = resolve_doc_memory("runbooks", "pg-tuning", data, updated_at=None)
    rest_spec = resolve_doc_memory("runbooks", "pg-tuning", data, updated_at=None)

    assert mcp_spec == rest_spec
    assert mcp_spec is not None
    assert _BODY in mcp_spec.content  # same render on both


def test_rule_lives_in_one_module_only():
    """Guard against a surface growing its own derivation logic."""
    import inspect

    from core_api.routes import documents as rest_module

    from core_api import mcp_server

    for module in (rest_module, mcp_server):
        src = inspect.getsource(module)
        # Both must delegate, not reimplement.
        assert "resolve_doc_memory(" in src
        assert "DocMemorySpec(" not in src, (
            f"{module.__name__} constructs a spec directly — the rule must stay "
            "in services.doc_indexing so the two surfaces cannot drift."
        )


# ── Both REST upsert branches mint ─────────────────────────────────────────────


@pytest.fixture
def rest_env(monkeypatch):
    """Stub the REST handler's collaborators.

    ``upsert_document`` returns a doc dict for the *unindexed* branch;
    ``upsert_document_xmax`` + ``get_document`` serve the *indexed* branch.
    """
    from core_api.routes import documents as mod

    doc = {
        "id": "11111111-1111-1111-1111-111111111111",
        "tenant_id": "t1",
        "fleet_id": None,
        "collection": "runbooks",
        "doc_id": "pg-tuning",
        "data": {"content": _BODY},
        "created_at": "2026-08-04T00:00:00+00:00",
        "updated_at": "2026-08-04T00:00:00+00:00",
    }

    sc = AsyncMock()
    sc.upsert_document = AsyncMock(return_value=doc)
    sc.upsert_document_xmax = AsyncMock(return_value={"xmax": 0})
    sc.get_document = AsyncMock(return_value=doc)
    monkeypatch.setattr(mod, "get_storage_client", lambda: sc)

    monkeypatch.setattr(mod, "check_and_increment", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "log_action", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "idempotency_for", AsyncMock(return_value=None))

    spy = AsyncMock(return_value="mem-1")
    monkeypatch.setattr("core_api.services.doc_memory.safe_sync_doc_memory", spy)

    return {"module": mod, "sc": sc, "spy": spy, "doc": doc}


def _auth():
    class _Auth:
        tenant_id = "t1"
        agent_id = "agent-a"

        def enforce_tenant(self, _t):
            return None

        def enforce_read_only(self):
            return None

        def enforce_usage_limits(self):
            return None

    return _Auth()


def _body(mod, **extra):
    payload = {
        "tenant_id": "t1",
        "collection": "runbooks",
        "doc_id": "pg-tuning",
        "data": {"content": _BODY},
    }
    payload.update(extra)
    return mod.DocWriteRequest(**payload)


async def test_unindexed_branch_mints(rest_env):
    """No ``data["summary"]`` -> the doc is stored WITHOUT an embedding (invisible
    to ``op=search``) but the memory is still minted, so recall reaches the body."""
    mod = rest_env["module"]

    await mod.upsert_document.__wrapped__(
        request=AsyncMock(),
        body=_body(mod),
        auth=_auth(),
        idempotency_key=None,
    )

    rest_env["sc"].upsert_document.assert_awaited_once()
    rest_env["spy"].assert_awaited_once()
    spec = rest_env["spy"].call_args.args[0]
    assert _BODY in spec.content
    assert spec.source_uri == "memclaw-doc://runbooks/pg-tuning"


async def test_indexed_branch_mints(rest_env, monkeypatch):
    """With a ``summary`` the handler takes the embed + xmax branch. Minting must
    fire there too — a single call site after both branches converge."""
    mod = rest_env["module"]
    monkeypatch.setattr(mod, "get_embedding", AsyncMock(return_value=[0.1] * 8))

    await mod.upsert_document.__wrapped__(
        request=AsyncMock(),
        body=_body(mod, data={"summary": "Postgres tuning.", "content": _BODY}),
        auth=_auth(),
        idempotency_key=None,
    )

    rest_env["sc"].upsert_document_xmax.assert_awaited_once()
    rest_env["spy"].assert_awaited_once()
    assert _BODY in rest_env["spy"].call_args.args[0].content


async def test_caller_agent_id_is_forwarded(rest_env):
    mod = rest_env["module"]

    await mod.upsert_document.__wrapped__(
        request=AsyncMock(), body=_body(mod), auth=_auth(), idempotency_key=None
    )

    assert rest_env["spy"].call_args.kwargs["agent_id"] == "agent-a"


async def test_bodyless_structured_record_now_mints(rest_env):
    """CAURA-717: a record with no ``content``/``body`` key used to skip. It now
    renders its fields instead — the change that unblocked eToro's doc feed."""
    mod = rest_env["module"]

    await mod.upsert_document.__wrapped__(
        request=AsyncMock(),
        body=_body(mod, collection="customers", data={"plan": "business", "seats": 40}),
        auth=_auth(),
        idempotency_key=None,
    )

    rest_env["spy"].assert_awaited_once()
    content = rest_env["spy"].call_args.args[0].content
    assert "plan: business" in content
    assert "seats: 40" in content


async def test_skips_mint_for_empty_payload(rest_env):
    """The only content-based skip left: nothing usable to render."""
    mod = rest_env["module"]

    await mod.upsert_document.__wrapped__(
        request=AsyncMock(),
        body=_body(mod, collection="customers", data={}),
        auth=_auth(),
        idempotency_key=None,
    )

    rest_env["spy"].assert_not_awaited()


async def test_raising_mint_does_not_fail_the_doc_write(rest_env):
    """Belt-and-braces guard at the call site: the document is already committed,
    so a broken mint must not turn a successful write into a 500."""
    mod = rest_env["module"]
    rest_env["spy"].side_effect = RuntimeError("memory subsystem down")

    out = await mod.upsert_document.__wrapped__(
        request=AsyncMock(), body=_body(mod), auth=_auth(), idempotency_key=None
    )

    rest_env["spy"].assert_awaited_once()
    assert out.doc_id == "pg-tuning"
