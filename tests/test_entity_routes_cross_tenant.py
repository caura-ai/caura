"""Unit tests for the cross-tenant read contract on entity routes (#24).

Previously ``routes/entities.py`` used ``enforce_tenant`` on every read
endpoint, which silently blocked cross-tenant credentials from reading
sibling tenants' entities — even though ``routes/memories.py`` already
widens memory reads via ``enforce_readable_tenant``. The fix aligns the
contract: ``list_entities``, ``get_graph``, and ``get_entity_route``
now use ``enforce_readable_tenant`` and emit a ``cross_tenant_read``
audit event when the requested tenant differs from the caller's home.

Write routes (``upsert_entity_route``, ``upsert_relation_route``)
intentionally retain ``enforce_tenant`` — writes pin to home_tenant.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import HTTPException

from core_api.auth import AuthContext
from core_api.routes import entities as entities_routes

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _auth(home: str = "tenant-home", readable: list[str] | None = None) -> AuthContext:
    """Build a non-admin AuthContext. Pass ``readable=["tenant-home",
    "tenant-other"]`` to model a cross-tenant credential."""
    return AuthContext(
        tenant_id=home,
        is_admin=False,
        agent_id="test-agent",
        readable_tenant_ids=readable,
    )


def _patch_storage(
    monkeypatch, entities: list[dict] | None = None, graph: dict | None = None
):
    """Stub get_storage_client() so route handlers don't touch the network."""
    sc = MagicMock(name="storage_client")
    sc.list_entities = AsyncMock(return_value=entities if entities is not None else [])
    sc.count_memories_per_entity = AsyncMock(return_value={})
    sc.get_full_graph = AsyncMock(
        return_value=graph if graph is not None else {"entities": [], "relations": []}
    )
    monkeypatch.setattr(entities_routes, "get_storage_client", lambda: sc)
    return sc


def _patch_audit(monkeypatch) -> AsyncMock:
    """Stub log_cross_tenant_read with an AsyncMock so tests can assert
    on emission shape."""
    spy = AsyncMock(name="log_cross_tenant_read")
    monkeypatch.setattr(entities_routes, "log_cross_tenant_read", spy)
    return spy


# ---------------------------------------------------------------------------
# Contract drift fix — list_entities
# ---------------------------------------------------------------------------


async def test_list_entities_single_tenant_blocked_from_foreign(monkeypatch):
    """A single-tenant credential still gets 403 when reading a
    different tenant — the gate moved from ``enforce_tenant`` to
    ``enforce_readable_tenant`` but the readable set for a single-tenant
    key is exactly ``[home_tenant]``, so foreign reads still fail."""
    _patch_storage(monkeypatch)
    auth = _auth(home="tenant-A")  # readable = ["tenant-A"]
    with pytest.raises(HTTPException) as exc:
        await entities_routes.list_entities(
            tenant_id="tenant-B",
            fleet_id=None,
            entity_type=None,
            search=None,
            limit=50,
            auth=auth,
            db=MagicMock(),
        )
    assert exc.value.status_code == 403


async def test_list_entities_cross_tenant_credential_allowed(monkeypatch):
    """A cross-tenant credential CAN now read sibling tenants — the
    behaviour that was previously broken (entity reads blocked while
    memory reads worked)."""
    sc = _patch_storage(monkeypatch, entities=[{"id": "e1", "tenant_id": "tenant-B"}])
    spy = _patch_audit(monkeypatch)
    auth = _auth(home="tenant-A", readable=["tenant-A", "tenant-B"])

    result = await entities_routes.list_entities(
        tenant_id="tenant-B",
        fleet_id=None,
        entity_type=None,
        search=None,
        limit=50,
        auth=auth,
        db=MagicMock(),
    )
    assert sc.list_entities.await_count == 1
    assert isinstance(result, list) and len(result) == 1
    # Cross-tenant read emits an audit event TO the source tenant.
    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs["home_tenant_id"] == "tenant-A"
    assert kwargs["source_tenants"] == ["tenant-B"]
    assert kwargs["surface"] == "rest_entities_list"
    assert kwargs["result_count_by_tenant"] == {"tenant-B": 1}


async def test_list_entities_home_tenant_does_not_audit(monkeypatch):
    """Reading the home tenant must NOT emit a cross_tenant_read event
    even if the credential is technically cross-tenant capable — the
    audit is for the act of reading FROM a sibling, not from home."""
    _patch_storage(monkeypatch)
    spy = _patch_audit(monkeypatch)
    auth = _auth(home="tenant-A", readable=["tenant-A", "tenant-B"])

    await entities_routes.list_entities(
        tenant_id="tenant-A",
        fleet_id=None,
        entity_type=None,
        search=None,
        limit=50,
        auth=auth,
        db=MagicMock(),
    )
    spy.assert_not_awaited()


async def test_list_entities_single_tenant_does_not_audit(monkeypatch):
    """A single-tenant credential reading its own tenant must not
    emit an audit event. ``is_cross_tenant_read`` is False for it."""
    _patch_storage(monkeypatch)
    spy = _patch_audit(monkeypatch)
    auth = _auth(home="tenant-A")  # readable = ["tenant-A"]

    await entities_routes.list_entities(
        tenant_id="tenant-A",
        fleet_id=None,
        entity_type=None,
        search=None,
        limit=50,
        auth=auth,
        db=MagicMock(),
    )
    spy.assert_not_awaited()


# ---------------------------------------------------------------------------
# Contract drift fix — get_graph
# ---------------------------------------------------------------------------


async def test_get_graph_cross_tenant_credential_allowed(monkeypatch):
    sc = _patch_storage(
        monkeypatch,
        graph={"entities": [{"id": "e1"}], "relations": [{"id": "r1"}]},
    )
    spy = _patch_audit(monkeypatch)
    auth = _auth(home="tenant-A", readable=["tenant-A", "tenant-B"])

    await entities_routes.get_graph(
        tenant_id="tenant-B",
        fleet_id=None,
        auth=auth,
        db=MagicMock(),
    )
    assert sc.get_full_graph.await_count == 1
    spy.assert_awaited_once()
    assert spy.await_args.kwargs["surface"] == "rest_graph"
    # Count is entities + relations, mirroring the audit surface contract.
    assert spy.await_args.kwargs["result_count_by_tenant"] == {"tenant-B": 2}


async def test_get_graph_single_tenant_blocked_from_foreign(monkeypatch):
    _patch_storage(monkeypatch)
    auth = _auth(home="tenant-A")
    with pytest.raises(HTTPException) as exc:
        await entities_routes.get_graph(
            tenant_id="tenant-B",
            fleet_id=None,
            auth=auth,
            db=MagicMock(),
        )
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Contract drift fix — get_entity_route (single item)
# ---------------------------------------------------------------------------


_FAKE_ENTITY_ID = UUID("11111111-1111-1111-1111-111111111111")


async def test_get_entity_cross_tenant_credential_allowed(monkeypatch):
    """Single-item entity GET widens to readable_tenant_ids — matches
    ``GET /memories/{memory_id}`` widening."""
    spy = _patch_audit(monkeypatch)
    fake_entity = MagicMock(name="entity")
    monkeypatch.setattr(
        entities_routes, "get_entity", AsyncMock(return_value=fake_entity)
    )
    auth = _auth(home="tenant-A", readable=["tenant-A", "tenant-B"])

    result = await entities_routes.get_entity_route(
        entity_id=_FAKE_ENTITY_ID,
        tenant_id="tenant-B",
        auth=auth,
        db=MagicMock(),
    )
    assert result is fake_entity
    spy.assert_awaited_once()
    assert spy.await_args.kwargs["surface"] == "rest_entity_get"
    assert spy.await_args.kwargs["result_count_by_tenant"] == {"tenant-B": 1}


async def test_get_entity_not_found_does_not_audit(monkeypatch):
    """A 404 must not emit a cross_tenant_read event — auditing a
    non-existent entity would leak the existence of entities by their
    presence/absence in the audit log."""
    spy = _patch_audit(monkeypatch)
    monkeypatch.setattr(entities_routes, "get_entity", AsyncMock(return_value=None))
    auth = _auth(home="tenant-A", readable=["tenant-A", "tenant-B"])

    with pytest.raises(HTTPException) as exc:
        await entities_routes.get_entity_route(
            entity_id=_FAKE_ENTITY_ID,
            tenant_id="tenant-B",
            auth=auth,
            db=MagicMock(),
        )
    assert exc.value.status_code == 404
    spy.assert_not_awaited()


async def test_get_entity_single_tenant_blocked_from_foreign(monkeypatch):
    monkeypatch.setattr(entities_routes, "get_entity", AsyncMock())
    auth = _auth(home="tenant-A")
    with pytest.raises(HTTPException) as exc:
        await entities_routes.get_entity_route(
            entity_id=_FAKE_ENTITY_ID,
            tenant_id="tenant-B",
            auth=auth,
            db=MagicMock(),
        )
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Write paths remain pinned to home_tenant
# ---------------------------------------------------------------------------


def test_upsert_entity_route_still_uses_enforce_tenant():
    """The write path must NOT have widened — confirm by reading the
    handler source. Belt-and-braces guard against future refactors that
    might over-broaden the alignment to write paths."""
    import inspect

    src = inspect.getsource(entities_routes.upsert_entity_route)
    assert "enforce_tenant" in src
    assert "enforce_readable_tenant" not in src


def test_upsert_relation_route_still_uses_enforce_tenant():
    import inspect

    src = inspect.getsource(entities_routes.upsert_relation_route)
    assert "enforce_tenant" in src
    assert "enforce_readable_tenant" not in src
