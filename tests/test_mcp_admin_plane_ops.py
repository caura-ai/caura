"""The two MCP ops whose REST twins are on the tenant's admin plane.

``caura_manage op=bulk_delete`` is ``POST /memories/bulk-delete`` and
``caura_doc op=delete`` is ``DELETE /documents/...``. On REST,
``enforce_tenant_admin_credential`` refuses an install credential and a
cross-tenant one, and the enterprise gateway refuses a person whose org role is
``member`` by matching the path. Every tool call arrives as ``/mcp``, so no
gateway can tell these two ops from a read: this surface refuses the same
callers itself, before any storage call.

Every other caller keeps both ops, and the single-row delete and the other doc
ops are not gated here, as their REST twins are not.
"""

from __future__ import annotations

from contextvars import ContextVar
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from core_api import mcp_server
from tests._mcp_test_helpers import (
    is_error_envelope,
    parse_envelope,
    stub_storage_client,
)

pytestmark = [pytest.mark.unit]


async def _bulk_delete():
    return await mcp_server.caura_manage(op="bulk_delete", memory_ids=[str(uuid4())])


async def _doc_delete():
    return await mcp_server.caura_doc(
        op="delete", collection="customers", doc_id="acme"
    )


# op -> (call, the storage method that does the delete)
OPS = {
    "bulk_delete": (_bulk_delete, "soft_delete_by_ids"),
    "doc_delete": (_doc_delete, "delete_document"),
}


@pytest.fixture
def storage(mcp_env, monkeypatch):
    mcp_env["service"]("log_action")
    monkeypatch.setattr(
        "core_api.services.doc_memory.safe_unmint_doc_memory", AsyncMock(return_value=0)
    )
    return stub_storage_client(monkeypatch, soft_delete_by_ids=1, delete_document=True)


def _caller(monkeypatch, *, kind=None, readable=None, role=None):
    """The gateway-stamped attributes of the caller, as the middleware sets them.

    A fresh ``ContextVar`` per attribute, set to the value (the handlers read
    them with ``.get(None)``, which ignores a var's own default), so nothing
    leaks into the next test. ``raising=False`` for the org role: the var did
    not exist before this gate, and without it these tests fail on the missing
    refusal rather than on an ``AttributeError``.
    """
    for name, value in (
        ("_credential_kind_var", kind),
        ("_readable_tenant_ids_var", readable),
        ("_org_role_var", role),
    ):
        var: ContextVar = ContextVar(name)
        var.set(value)
        monkeypatch.setattr(mcp_server, name, var, raising=False)


REFUSED = {
    "install_credential": ({"kind": "install_credential"}, "CREDENTIAL_KIND_FORBIDDEN"),
    "cross_tenant": ({"kind": "cross_tenant"}, "CREDENTIAL_KIND_FORBIDDEN"),
    "read_scope_spans_tenants": (
        {"kind": "user_api_key", "readable": ["test-tenant", "other-tenant"]},
        "CREDENTIAL_KIND_FORBIDDEN",
    ),
    "org_member": ({"role": "member"}, "ORG_ADMIN_REQUIRED"),
}

ALLOWED = {
    "tenant_key": {"kind": "user_api_key"},
    "home_tenant_only_read_scope": {
        "kind": "user_api_key",
        "readable": ["test-tenant"],
    },
    "agent_key": {"kind": "agent_key"},
    "org_admin": {"role": "admin"},
    "no_gateway": {},
}


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("caller", REFUSED)
async def test_refused_before_any_delete(storage, monkeypatch, op, caller):
    attrs, code = REFUSED[caller]
    _caller(monkeypatch, **attrs)
    call, method = OPS[op]
    out = await call()
    assert is_error_envelope(out)
    assert parse_envelope(out)["error"]["code"] == code
    getattr(storage, method).assert_not_awaited()


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("caller", ALLOWED)
async def test_other_callers_keep_the_op(storage, monkeypatch, op, caller):
    _caller(monkeypatch, **ALLOWED[caller])
    call, method = OPS[op]
    out = await call()
    assert not is_error_envelope(out), parse_envelope(out)
    getattr(storage, method).assert_awaited_once()


async def test_single_memory_delete_is_not_gated(mcp_env, monkeypatch):
    """``DELETE /memories/{id}`` is not an admin-plane route, so neither is its twin."""
    _caller(monkeypatch, kind="cross_tenant", role="member")
    delete = mcp_env["service"]("soft_delete_memory")
    mcp_env["service"]("log_action")
    out = await mcp_server.caura_manage(op="delete", memory_id=str(uuid4()))
    assert not is_error_envelope(out), parse_envelope(out)
    delete.assert_awaited_once()


async def test_doc_write_is_not_gated(mcp_env, monkeypatch):
    _caller(monkeypatch, kind="cross_tenant", role="member")
    sc = stub_storage_client(monkeypatch, upsert_document_xmax={"xmax": 0})
    mcp_env["service"]("log_action")
    out = await mcp_server.caura_doc(
        op="write", collection="customers", doc_id="acme", data={"plan": "business"}
    )
    assert not is_error_envelope(out), parse_envelope(out)
    sc.upsert_document_xmax.assert_awaited_once()
